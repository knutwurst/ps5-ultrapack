using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using LibProsperoPkg;
using LibProsperoPkg.Content;
using LibProsperoPkg.PFS;
using LibProsperoPkg.PKG;
using LibProsperoPkg.PlayGo;

namespace PkgTool;

// ffpfsc-pkg-tool: thin CLI over drakmor's LibProsperoPkg 1.2.0 (a53 fpkg-gui 0.5).
// Purpose: fPKG extract / inspect / build for ps5-ultrapack.
// Ships as a self-contained osx-arm64 binary under backend/native/.

internal static partial class Program
{
    const string ToolVersion = "2.2.2";

    static int Main(string[] args)
    {
        try
        {
            RedirectMagickNative();                    // must run before ANY Magick.NET call
            RemoveOldBundleExtractions();
            if (args.Length == 0) { PrintUsage(); return 2; }
            return args[0].ToLowerInvariant() switch
            {
                "version" => CmdVersion(),
                "inspect" => CmdInspect(args),
                "list-inner" => CmdListInner(args),
                "extract-inner" => CmdExtract(args, inner: true),
                "extract-outer" => CmdExtract(args, inner: false),
                "build" => CmdBuild(args),
                "validate" => CmdValidate(args),
                "ampr-index" => CmdAmprIndex(args),
                "ps4-list" => CmdPs4List(args),
                "ps4-extract" => CmdPs4Extract(args),
                _ => Bad("unknown command: " + args[0]),
            };
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("[error] " + ex.GetType().Name + ": " + ex.Message);
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine(ex.StackTrace);
            return 1;
        }
    }

    static void PrintUsage()
    {
        Console.WriteLine("ffpfsc-pkg-tool <command> [args]");
        Console.WriteLine("  version");
        Console.WriteLine("  inspect       <pkg>                              [--json]");
        Console.WriteLine("  list-inner    <pkg> [--passcode P]");
        Console.WriteLine("  ampr-index    <folder>          (re)write <folder>/ampr_emu.index (AMPRIDX3) over its files");
        Console.WriteLine("      One JSON object on stdout: the /app0 tree (files with logical sizes, every");
        Console.WriteLine("      directory, CNT-lifted sce_sys metadata tagged \"source\":\"cnt\") — read via");
        Console.WriteLine("      random access, the inner image is NOT decoded as a whole.");
        Console.WriteLine("  extract-inner <pkg> <out-dir> [--passcode P]     [--json]");
        Console.WriteLine("  extract-inner <pkg> <out-dir> --members <file> [--passcode P] [--json]");
        Console.WriteLine("  ps4-list    <ps4.pkg>                            (JSON tree of a PS4 fake package's files)");
        Console.WriteLine("  ps4-extract <ps4.pkg> <out-dir> [--members <file>]  (all files, or the listed files/folders)");
        Console.WriteLine("      Selective: <file> lists one path per line (relative to /app0); a directory");
        Console.WriteLine("      means its whole subtree incl. empty folders. Prints '[####] NN% extract (path)'.");
        Console.WriteLine("  extract-outer <pkg> <out-dir> [--passcode P]     [--decompress|--no-decompress]");
        Console.WriteLine("  validate      <pkg> [--passcode P] [--temp DIR]  [--json]   (reads the package in place; --temp only");
        Console.WriteLine("                                                             holds the lifted sce_sys entries for a moment)");
        Console.WriteLine("      Diagnostic checklist: header magic + fields, CNT wrap, PFS bounds,");
        Console.WriteLine("      required sce_sys entries, param.json coherence, eboot fake-self magic.");
        Console.WriteLine("  build         <src-dir> <out-dir>");
        Console.WriteLine("      --content-id <36-char>       (required)");
        Console.WriteLine("      --title-id   <9-char>        (required, e.g. PPSA00000)");
        Console.WriteLine("      --title      <text>          (written to param.json if generated)");
        Console.WriteLine("      --version    <NN.NNN.NNN>    (default 01.000.000)");
        Console.WriteLine("      --passcode   <32-char>       (default 32 zeros)");
        Console.WriteLine("      --mode       none|zlib|kraken   inner-image codec (default none)");
        Console.WriteLine("      --kraken-backend automatic|builtin|publishingtools|uncompressed (default builtin)");
        Console.WriteLine("      --pubtools-dll <path>        libScePubTools.dll for kraken/publishingtools");
        Console.WriteLine("      --deterministic              byte-reproducible build");
        Console.WriteLine("      --temp <dir>                 intermediate files go here (default: $TMPDIR)");
        Console.WriteLine("      --level <n>                  compression level: Kraken -4..9, zlib 0..9 (default 7)");
        Console.WriteLine("      --playgo-chunks <1..255>     chunk count when the builder generates the PlayGo set (default 1). A source");
        Console.WriteLine("                                   sce_sys/playgo-chunk.dat overrides it: its chunk and scenario counts, file");
        Console.WriteLine("                                   assignments, labels and languages are kept, the image ranges are rebuilt.");
        Console.WriteLine("      --layout-pass on|off         LibProsperoPkg's inner-image layout pass (outer block coalescing + relocation");
        Console.WriteLine("                                   alignment adjustment; default on). off = the layout of the 1.1.x builds");
        Console.WriteLine("      --parallelism <n> / -j <n>   Kraken and outer-PFS workers (default 0 = one per core)");
        Console.WriteLine("      --fake-sign / --no-fake-sign fake-sign raw ELFs in source before packing (default ON; idempotent)");
        Console.WriteLine("      --no-ampr-index              keep the source's ampr_emu.index as it is (default: when fakelib/");
        Console.WriteLine("                                   libSceAmpr.sprx is shipped, rebuild the index over the packed files)");
        Console.WriteLine("      --regen-playgo               discard source sce_sys/playgo-*.dat and let the builder regenerate them");
        Console.WriteLine("                                   (a CORRUPT prepared set — wrong format, e.g. JSON under hash-table.dat — is");
        Console.WriteLine("                                   always discarded automatically; this forces it for valid-looking sets too)");
        Console.WriteLine("      --hdr-flag auto|on|off       param.json attribute bit 29 (HDR support). auto = keep what the source");
        Console.WriteLine("                                   declares (default; that is the publisher's intent), on = set it, off = clear");
        Console.WriteLine("                                   it. A console on \"HDR when supported\" switches output modes on this bit.");
        Console.WriteLine("      --retail-normalize / --no-retail-normalize");
        Console.WriteLine("                                   auto-upgrade a \"standard\" retail source (default ON):");
        Console.WriteLine("                                     param.json applicationDrmType stays as it is (the library stamps drm_type=16),");
        Console.WriteLine("                                     replace placeholder license.dat/info with a valid debug license,");
        Console.WriteLine("                                     add CNT entries 0x0400/0x0401 via IProsperoLicenseProvider");
        Console.WriteLine("");
        Console.WriteLine("  Default passcode 32 x '0'. Default output is a finalized debug image.");
        Console.WriteLine("  Every build works on a hard-link mirror of the source (next to the source folder when");
        Console.WriteLine("  that is writable, else in --temp); the source itself is never modified. Auto-fake-sign");
        Console.WriteLine("  scans for raw ELF magic (0x7F454C46) in eboot.bin, *.elf, *.prx, *.sprx and rewrites");
        Console.WriteLine("  them as SCE fake-selves in that mirror. SIGTERM/SIGINT cancel the build and remove the");
        Console.WriteLine("  mirror, the library's temp files and any partial .pkg (exit 143/130).");
    }

    static int Bad(string m) { Console.Error.WriteLine("[usage] " + m); PrintUsage(); return 2; }

    /// <summary>The value of the option at args[i]; throws when the option is the last argument.
    /// Every value-taking option of every command goes through here so "--members" without a
    /// file cannot fall through to a full extract and "--mode" alone cannot NRE.</summary>
    static int CmdAmprIndex(string[] args)
    {
        if (args.Length != 2 || !Directory.Exists(args[1])) return Bad("usage: ampr-index <folder>");
        int rows = AmprIndex.Write(args[1]);
        Console.WriteLine(rows > 0 ? $"OK — wrote {AmprIndex.FileName} ({rows:N0} file(s))" : "nothing to index");
        return 0;
    }

    static string Need(string[] args, ref int i, string name)
    {
        if (i + 1 >= args.Length) throw new ArgumentException(name + " needs a value");
        return args[++i];
    }

    static int CmdVersion()
    {
        var asm = typeof(ProsperoPkgReader).Assembly;
        var name = asm.GetName();
        Console.WriteLine("ffpfsc-pkg-tool " + ToolVersion);
        var info = asm.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        Console.WriteLine($"LibProsperoPkg: {name.Name} v{name.Version}" + (info != null ? $" ({info})" : ""));
        Console.WriteLine($".NET: {Environment.Version}");
        Console.WriteLine($"Host: {Environment.OSVersion.Platform} {Environment.OSVersion.Version} ({System.Runtime.InteropServices.RuntimeInformation.OSArchitecture})");
        Console.WriteLine($"SHA3-256 (System): {System.Security.Cryptography.SHA3_256.IsSupported}");
        return 0;
    }

    static int CmdInspect(string[] args)
    {
        if (args.Length < 2) return Bad("inspect needs <pkg>");
        var pkg = args[1];
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        bool json = false;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--json") json = true;
            else return Bad("unknown inspect flag: " + args[i]);
        }
        var type = ProsperoPkgReader.DetectType(pkg);
        var pkgObj = ProsperoPkgReader.Read(pkg);
        var info = new InspectDoc
        {
            path = Path.GetFullPath(pkg),
            size_bytes = new FileInfo(pkg).Length,
            package_type = type?.ToString(),
            content_id = pkgObj.Header?.ContentId,
            title_id = pkgObj.Header?.ContentId != null && pkgObj.Header.ContentId.Length >= 16
                ? pkgObj.Header.ContentId.Substring(7, 9) : null,
            drm_type = pkgObj.Header?.DrmType,
            content_type = pkgObj.Header?.ContentType,
            entry_count = pkgObj.Header?.EntryCount,
            sc_entry_count = pkgObj.Header?.ScEntryCount,
            finalized = pkgObj.Fih != null,
            fih = pkgObj.Fih == null ? null : new InspectFihDoc {
                is_official = pkgObj.Fih.IsOfficial,
                signed_byte = pkgObj.Fih.SignedByte,
                pfs_image_offset = pkgObj.Fih.PfsImageOffset,
                pfs_image_size = pkgObj.Fih.PfsImageSize,
                embedded_cnt_off = pkgObj.Fih.EmbeddedCntOffset,
                inner_blocks = pkgObj.Fih.InnerImageBlockCount,
                metadata_blocks = pkgObj.Fih.MetadataBlockCount,
                naps_layout_size = pkgObj.Fih.NapsLayoutSize,
            },
            // The CNT entries by name (sce_sys metadata such as icons, sound, trophy and
            // PlayGo files live here, not in the inner image).
            entries = pkgObj.Entries.Select(e => new InspectEntryDoc
            {
                name = e.Name ?? (EntryNames.IdToName.TryGetValue((EntryId)e.RawId, out var nm) ? nm : $"0x{e.RawId:X4}"),
                id = e.RawId,
                size = e.DataSize,
            }).ToList(),
        };
        if (json) Console.WriteLine(JsonSerializer.Serialize(info, PkgToolJsonContext.Indented.InspectDoc));
        else
        {
            Console.WriteLine($"path         : {info.path}");
            Console.WriteLine($"size         : {info.size_bytes:N0} bytes");
            Console.WriteLine($"package type : {info.package_type}");
            Console.WriteLine($"content id   : {info.content_id}");
            Console.WriteLine($"title id     : {info.title_id}");
            Console.WriteLine($"entries      : {info.entry_count} ({info.sc_entry_count} system)");
            Console.WriteLine($"finalized    : {info.finalized}");
            if (info.fih != null)
            {
                Console.WriteLine($"fih.official : {info.fih.is_official}");
                Console.WriteLine($"fih.pfs off  : 0x{info.fih.pfs_image_offset:X}");
                Console.WriteLine($"fih.pfs size : {info.fih.pfs_image_size:N0}");
                Console.WriteLine($"fih.naps len : {info.fih.naps_layout_size:N0}");
            }
        }
        return 0;
    }

    static int CmdExtract(string[] args, bool inner)
    {
        if (args.Length < 3) return Bad("extract needs <pkg> <out-dir>");
        var pkg = args[1]; var outDir = args[2];
        string passcode = new string('0', 32);
        bool decompress = true;
        bool json = false;
        bool mergeCnt = true;   // for extract-inner: also drop param.json/icon0/playgo-* into sce_sys/
        string? membersFile = null;
        for (int i = 3; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--passcode": passcode = Need(args, ref i, "--passcode"); break;
                case "--decompress": decompress = true; break;
                case "--no-decompress": decompress = false; break;
                case "--json": json = true; break;
                case "--no-merge-cnt": mergeCnt = false; break;
                case "--members": membersFile = Need(args, ref i, "--members"); break;
                default: return Bad($"unknown extract-{(inner ? "inner" : "outer")} flag: {args[i]}");
            }
        }
        if (membersFile != null)
        {
            if (!inner) return Bad("--members is only supported by extract-inner");
            return CmdExtractMembers(pkg, outDir, passcode, membersFile, json);
        }
        Directory.CreateDirectory(outDir);
        outDir = RealPath(outDir);
        Console.Error.WriteLine($"[info] extract-{(inner ? "inner" : "outer")}  {pkg} -> {outDir}");
        var files = inner
            ? ProsperoPackageArchive.ExtractInnerFiles(pkg, outDir, passcode, decompressFiles: decompress)
            : ProsperoPackageArchive.ExtractOuterFiles(pkg, outDir, passcode, decompress: decompress);

        // For inner-image extraction: sce_sys/param.json, sce_sys/icon0.png and the
        // sce_sys/playgo-*.dat files live in the CNT entry table, not the inner PFS.
        // Without them, mkpfs pack-to-ffpfsc would refuse the /app0 folder. Merge them
        // into the extracted tree so downstream tools see a complete PS5 game folder.
        int mergedCount = 0;
        if (inner && mergeCnt)
        {
            var tmpCnt = Path.Combine(outDir, ".cnt-tmp-" + Guid.NewGuid().ToString("N"));
            try
            {
                var errors = new List<string>();
                foreach (var kv in LiftCntSceSys(pkg, passcode, tmpCnt, errors))
                {
                    var dst = SafeTarget(outDir, kv.Key);
                    Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                    File.Copy(kv.Value, dst, overwrite: true);
                    mergedCount++;
                }
                foreach (var e in errors) Console.Error.WriteLine("[warn] " + e + " — extract still succeeded but /app0 may be incomplete.");
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidDataException)
            {
                Console.Error.WriteLine("[warn] CNT metadata merge failed: " + ex.Message + " — extract still succeeded but /app0 may be incomplete.");
            }
            finally
            {
                try { Directory.Delete(tmpCnt, recursive: true); } catch { }
            }
        }

        if (json)
        {
            Console.WriteLine(JsonSerializer.Serialize(new ExtractResultDoc {
                extracted = files.Count,
                cnt_merged = mergedCount,
                output = Path.GetFullPath(outDir), files = files.ToList()
            }, PkgToolJsonContext.Indented.ExtractResultDoc));
        }
        else
        {
            string extra = (inner && mergedCount > 0) ? $" (+{mergedCount} sce_sys metadata)" : "";
            Console.WriteLine($"OK — extracted {files.Count} file(s){extra} to {Path.GetFullPath(outDir)}");
        }
        return 0;
    }

    /// <summary>True for a CNT entry that is a file of /app0/sce_sys (param.json, icons, DDS,
    /// sound, trophy/uds data, PlayGo, license, nptitle, …). False for the package's own
    /// bookkeeping entries (".digests", ".entry_keys", ".metas", …) and for entries the name
    /// table does not name (shown as a bare id): those are generated by the builder and have no
    /// place in a game folder.</summary>
    static bool IsSceSysCntName(string rel)
    {
        if (rel.Length == 0 || rel.Split('/').Any(s => s.Length == 0 || s[0] == '.' || s == "..")) return false;
        var leaf = Path.GetFileName(rel);
        if (!leaf.Contains('.')) return false;                                   // unnamed id, no extension
        // Unnamed ids as the extractor writes them: "entry-0000040a.bin" (also accept "0x040A[.bin]").
        var stem = Path.GetFileNameWithoutExtension(leaf);
        if (stem.StartsWith("entry-", StringComparison.OrdinalIgnoreCase)) stem = stem[6..];
        else if (stem.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) stem = stem[2..];
        else return true;
        return !(stem.Length > 0 && stem.All(Uri.IsHexDigit));
    }

    /// <summary>
    /// Extracts the CNT entries into <paramref name="tmpDir"/> and returns "sce_sys/&lt;name&gt;" -> temp
    /// file for every sce_sys entry the package carries. The full extract, list-inner and the
    /// selective extract all use this, so the three agree on what "the inner tree" contains, and
    /// a package unpacks to the complete sce_sys the builder took in (minus what it drops by
    /// design). Failures are reported into <paramref name="errors"/> (the inner tree is still
    /// usable without them, matching the full extract's [warn] behaviour).
    /// </summary>
    static Dictionary<string, string> LiftCntSceSys(string pkg, string passcode, string tmpDir, List<string> errors)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        try
        {
            Directory.CreateDirectory(tmpDir);
            tmpDir = RealPath(tmpDir);
            ProsperoPackageArchive.ExtractCntEntries(pkg, tmpDir, passcode, includeEncrypted: true);
            foreach (var src in Directory.EnumerateFiles(tmpDir, "*", SearchOption.AllDirectories))
            {
                var rel = Path.GetRelativePath(tmpDir, src).Replace('\\', '/');
                if (IsSceSysCntName(rel)) result["sce_sys/" + rel] = src;
                else if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                    Console.Error.WriteLine("[trace] CNT entry not lifted: " + rel);
            }
        }
        catch (Exception ex)
        {
            errors.Add("CNT metadata unavailable: " + ex.Message);
        }
        return result;
    }

    // Every --json document goes through the source-generated serializer (PkgToolJsonContext
    // below). It was introduced while the binary was still published trimmed (which switches
    // reflection-based System.Text.Json off); PublishTrimmed is false today, the context is kept
    // because it works regardless of the publish settings. The classes mirror the anonymous
    // types the commands used before: same field names, same number/bool/null shapes.
    internal sealed class InspectDoc
    {
        public string path { get; set; } = "";
        public long size_bytes { get; set; }
        public string? package_type { get; set; }
        public string? content_id { get; set; }
        public string? title_id { get; set; }
        public uint? drm_type { get; set; }
        public uint? content_type { get; set; }
        public uint? entry_count { get; set; }
        public ushort? sc_entry_count { get; set; }
        public bool finalized { get; set; }
        public InspectFihDoc? fih { get; set; }
        public List<InspectEntryDoc>? entries { get; set; }
    }

    internal sealed class InspectEntryDoc
    {
        public string name { get; set; } = "";
        public uint id { get; set; }
        public uint size { get; set; }
    }

    internal sealed class InspectFihDoc
    {
        public bool is_official { get; set; }
        public byte signed_byte { get; set; }
        public ulong pfs_image_offset { get; set; }
        public ulong pfs_image_size { get; set; }
        public ulong embedded_cnt_off { get; set; }
        public uint inner_blocks { get; set; }
        public uint metadata_blocks { get; set; }
        public ulong naps_layout_size { get; set; }
    }

    internal sealed class ExtractResultDoc
    {
        public int extracted { get; set; }
        public int cnt_merged { get; set; }
        public string output { get; set; } = "";
        public List<string> files { get; set; } = new();
    }

    internal sealed class ValidateReportDoc
    {
        public int pass { get; set; }
        public int warn { get; set; }
        public int fail { get; set; }
        public List<ValidateResult> results { get; set; } = new();
    }

    internal sealed class InnerEntry
    {
        public string path { get; set; } = "";
        public string type { get; set; } = "file";   // "file" | "dir"
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public long? size { get; set; }
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public string? source { get; set; }             // "pfs" | "cnt" (files only)
    }

    internal sealed class ListInnerDoc
    {
        public string root { get; set; } = "";
        public List<InnerEntry> entries { get; set; } = new();
        public int file_count { get; set; }
        public int dir_count { get; set; }
        public List<string> errors { get; set; } = new();
    }

    internal sealed class MembersResultDoc
    {
        public int extracted { get; set; }
        public int dirs { get; set; }
        public int cnt_extracted { get; set; }
        public string output { get; set; } = "";
        public List<string> files { get; set; } = new();
        public List<string> errors { get; set; } = new();
    }

    /// <summary>Rejects the path shapes the library rejects (empty, '.', '..' segments).</summary>
    static string SafeRelative(string rel)
    {
        var norm = rel.Replace('\\', '/').Trim().Trim('/');
        if (norm.Length == 0 || norm.Split('/').Any(p => p.Length == 0 || p == "." || p == ".."))
            throw new InvalidDataException("unsafe package path: " + rel);
        return norm;
    }

    static string SafeTarget(string root, string rel)
    {
        var rootFull = Path.GetFullPath(root) + Path.DirectorySeparatorChar;
        var full = Path.GetFullPath(Path.Combine(root, rel.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(rootFull, StringComparison.Ordinal))
            throw new InvalidDataException("package path escapes output directory: " + rel);
        return full;
    }

    /// <summary>
    /// The /app0 tree as list-inner reports it and as extract-inner --members resolves it:
    /// inner-PFS files + dirs, plus the CNT-lifted sce_sys files (which win over a same-named
    /// PFS file, exactly like the full extract's overwrite-merge).
    /// </summary>
    static SortedDictionary<string, InnerEntry> BuildInnerTree(InnerImage img, IReadOnlyDictionary<string, string> cnt)
    {
        var tree = new SortedDictionary<string, InnerEntry>(StringComparer.Ordinal);
        void AddDirs(string filePath)
        {
            int idx = -1;
            while ((idx = filePath.IndexOf('/', idx + 1)) >= 0)
            {
                var d = filePath.Substring(0, idx);
                if (!tree.ContainsKey(d)) tree[d] = new InnerEntry { path = d, type = "dir" };
            }
        }
        foreach (var d in img.AllDirs())
        {
            var rel = SafeRelative(img.RelativePath(d));
            tree[rel] = new InnerEntry { path = rel, type = "dir" };
        }
        foreach (var f in img.Pfs.GetAllFiles())
        {
            var rel = SafeRelative(img.RelativePath(f));
            AddDirs(rel);
            tree[rel] = new InnerEntry { path = rel, type = "file", size = f.size, source = "pfs" };
        }
        foreach (var kv in cnt)
        {
            AddDirs(kv.Key);
            tree[kv.Key] = new InnerEntry { path = kv.Key, type = "file", size = new FileInfo(kv.Value).Length, source = "cnt" };
        }
        return tree;
    }

    static int CmdListInner(string[] args)
    {
        if (args.Length < 2) return Bad("list-inner needs <pkg>");
        var pkg = args[1];
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        string passcode = new string('0', 32);
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--passcode") passcode = Need(args, ref i, "--passcode");
            else return Bad("unknown list-inner flag: " + args[i]);
        }

        var errors = new List<string>();
        var tmpCnt = Path.Combine(RealPath(Path.GetTempPath()), "fpkg-list-cnt-" + Guid.NewGuid().ToString("N"));
        try
        {
            using var img = new InnerImage(pkg, passcode);
            var cnt = LiftCntSceSys(pkg, passcode, tmpCnt, errors);
            var tree = BuildInnerTree(img, cnt);
            int files = 0, dirs = 0;
            foreach (var e in tree.Values) { if (e.type == "dir") dirs++; else files++; }
            var doc = new ListInnerDoc
            {
                root = Path.GetFileName(pkg),
                entries = tree.Values.ToList(),
                file_count = files,
                dir_count = dirs,
                errors = errors,
            };
            Console.WriteLine(JsonSerializer.Serialize(doc, PkgToolJsonContext.Default.ListInnerDoc));
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine($"[trace] list-inner: logical image {img.LogicalSize:N0} B, superblock @0x{img.SuperblockOffset:X}, " +
                                        $"{img.RangeCalls} range decodes / {img.RangeBytes:N0} B, span decoder bound={NapsBlockReader.UsesSpanDecoder}");
            return 0;
        }
        finally
        {
            try { if (Directory.Exists(tmpCnt)) Directory.Delete(tmpCnt, recursive: true); } catch { }
        }
    }

    static int CmdExtractMembers(string pkg, string outDir, string passcode, string membersFile, bool json)
    {
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        if (!File.Exists(membersFile)) throw new FileNotFoundException("members file not found", membersFile);
        var wanted = new List<string>();
        foreach (var raw in File.ReadAllLines(membersFile, System.Text.Encoding.UTF8))
        {
            var m = raw.Replace('\\', '/').Trim().Trim('/');
            if (m.Length > 0 && !wanted.Contains(m)) wanted.Add(m);
        }
        if (wanted.Count == 0) return Bad("--members file lists no paths");
        bool Under(string rel) => wanted.Any(m => rel == m || rel.StartsWith(m + "/", StringComparison.Ordinal));

        Directory.CreateDirectory(outDir);
        Console.Error.WriteLine($"[info] extract-inner (members)  {pkg} -> {outDir}  ({wanted.Count} member(s))");
        var errors = new List<string>();
        var tmpCnt = Path.Combine(RealPath(outDir), ".cnt-tmp-" + Guid.NewGuid().ToString("N"));
        try
        {
            // 4 MiB cache blocks: metadata walks need few of them and file data streams through
            // the chunked fast path anyway, so the plan-rebuild cost per DecompressRange amortizes.
            using var img = new InnerImage(pkg, passcode, cacheBlockSize: 4 << 20, cacheBlocks: 8);
            var cnt = LiftCntSceSys(pkg, passcode, tmpCnt, errors);
            foreach (var e in errors) Console.Error.WriteLine("[warn] " + e);
            var tree = BuildInnerTree(img, cnt);

            var dirTargets = tree.Values.Where(e => e.type == "dir" && Under(e.path)).Select(e => e.path).ToList();
            var fileTargets = tree.Values.Where(e => e.type == "file" && Under(e.path)).ToList();
            foreach (var m in wanted)
                if (!tree.ContainsKey(m)) Console.Error.WriteLine($"[warn] member not found in the image: {m}");
            if (dirTargets.Count == 0 && fileTargets.Count == 0)
            {
                Console.Error.WriteLine("[ERROR] None of the requested items were found in the image.");
                return 1;
            }

            // Directories first (parent-first by sort order) so empty ones survive.
            foreach (var d in dirTargets) Directory.CreateDirectory(SafeTarget(outDir, d));
            // Parents of selected files that were not themselves selected.
            foreach (var f in fileTargets)
                Directory.CreateDirectory(Path.GetDirectoryName(SafeTarget(outDir, f.path))!);

            long total = fileTargets.Sum(e => e.size ?? 0), done = 0;
            int lastPct = -1;
            var written = new List<string>();
            int cntCount = 0;
            void Progress(long delta, string rel)
            {
                done += delta;
                int pct = total > 0 ? (int)Math.Min(99, done * 100 / total) : 99;
                if (pct != lastPct)
                {
                    lastPct = pct;
                    Console.WriteLine($"[####] {pct}% extract ({rel})");
                }
            }
            foreach (var e in fileTargets)
            {
                var dst = SafeTarget(outDir, e.path);
                if (e.source == "cnt")
                {
                    File.Copy(cnt[e.path], dst, overwrite: true);
                    cntCount++;
                    Progress(e.size ?? 0, e.path);
                }
                else
                {
                    var node = img.Pfs.GetFile(e.path) ?? throw new InvalidDataException("inner file vanished: " + e.path);
                    using var fs = new FileStream(dst, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20);
                    img.CopyFile(node, fs, n => Progress(n, e.path));
                }
                written.Add(e.path);
            }
            Console.WriteLine("[####] 100% extract");
            if (json)
            {
                Console.WriteLine(JsonSerializer.Serialize(new MembersResultDoc
                {
                    extracted = written.Count,
                    dirs = dirTargets.Count,
                    cnt_extracted = cntCount,
                    output = Path.GetFullPath(outDir),
                    files = written,
                    errors = errors,
                }, PkgToolJsonContext.Indented.MembersResultDoc));
            }
            else
            {
                string extra = cntCount > 0 ? $" (+{cntCount} sce_sys metadata)" : "";
                Console.WriteLine($"OK — extracted {written.Count} file(s) and {dirTargets.Count} folder(s){extra} to {Path.GetFullPath(outDir)}");
            }
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine($"[trace] members: {img.RangeCalls} range decodes / {img.RangeBytes:N0} B for {done:N0} B of output");
            return 0;
        }
        finally
        {
            try { if (Directory.Exists(tmpCnt)) Directory.Delete(tmpCnt, recursive: true); } catch { }
        }
    }

    static int CmdValidate(string[] args)
    {
        if (args.Length < 2) return Bad("validate needs <pkg>");
        var pkg = args[1];
        bool json = false;
        string passcode = new string('0', 32);
        string? tempRoot = null;          // --temp: where the lifted CNT entries go (never the user's folders)
        for (int i = 2; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--json": json = true; break;
                case "--passcode": passcode = Need(args, ref i, "--passcode"); break;
                case "--temp": tempRoot = Need(args, ref i, "--temp"); break;
                default: return Bad("unknown validate flag: " + args[i]);
            }
        }
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        var checks = new System.Collections.Generic.List<ValidateResult>();
        // The table is printed at the end (aligned columns), so the caller sees nothing until
        // then; these one-word milestones let it move a progress bar meanwhile.
        void Step(string name) { if (!json) Console.WriteLine($"[validate] {name}"); }
        void Ok(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "pass", Message = msg });
        void Warn(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "warn", Message = msg });
        void Fail(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "fail", Message = msg });

        Step("file");
        long sz = new FileInfo(pkg).Length;
        // A finalized image starts with a 64 KiB FIH block, so anything smaller cannot be one.
        if (sz >= 0x10000) Ok("file", $"{sz:N0} bytes on disk");
        else               Fail("file", $"{sz:N0} bytes on disk — smaller than one 64 KiB image block");

        // Magic
        using (var fs = new FileStream(pkg, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            var m = new byte[4]; int got = fs.Read(m, 0, 4);
            if (got < 4) { Fail("magic", "file shorter than 4 bytes"); Report(checks, json); return 1; }
            string magic = (m[0] == 0x7F && m[1] == 'C' && m[2] == 'N' && m[3] == 'T') ? "CNT"
                         : (m[0] == 0x7F && m[1] == 'F' && m[2] == 'I' && m[3] == 'H') ? "FIH"
                         : $"UNKNOWN ({m[0]:X2} {m[1]:X2} {m[2]:X2} {m[3]:X2})";
            if (magic == "FIH")      Ok("magic", "\x7FFIH (finalized image, installable)");
            else if (magic == "CNT") Fail("magic", "\x7FCNT metadata container only — NOT installable, needs finalization");
            else                     Fail("magic", magic);
        }

        // Parse the container
        Step("header");
        ProsperoPkg pkgObj;
        try { pkgObj = ProsperoPkgReader.Read(pkg); }
        catch (Exception ex) { Fail("parse", "reader threw: " + ex.Message); Report(checks, json); return 1; }
        var h = pkgObj.Header;
        var fih = pkgObj.Fih;
        if (h == null) { Fail("header", "no CNT header parsed"); Report(checks, json); return 1; }
        Ok("header", $"content_id={h.ContentId} entries={h.EntryCount} sc_entries={h.ScEntryCount} drm=0x{h.DrmType:X} content_type=0x{h.ContentType:X}");

        // Content id format
        var cidRe = new System.Text.RegularExpressions.Regex("^[A-Z]{2}[0-9]{4}-[A-Z]{4}[0-9]{5}_00-[A-Z0-9]{16}$");
        if (h.ContentId.Length == 36 && cidRe.IsMatch(h.ContentId))
            Ok("content_id", "matches XX0000-XXXX00000_00-XXXXXXXXXXXXXXXX");
        else
            Fail("content_id", $"'{h.ContentId}' is not the expected 36-char pattern");

        // FIH
        if (fih == null) { Fail("finalized", "no FIH header — this is a metadata-only CNT, not installable"); }
        else
        {
            if (fih.SignedByte == 0x00)      Ok("fih.signed_byte", "0x00 (debug image, installable on debug consoles)");
            else if (fih.SignedByte == 0x80) Warn("fih.signed_byte", "0x80 (retail image — needs the console-provisioned image key)");
            else                             Fail("fih.signed_byte", $"unexpected value 0x{fih.SignedByte:X}");
            long imgEnd = checked((long)fih.PfsImageOffset + (long)fih.PfsImageSize);
            if (imgEnd <= sz && fih.PfsImageOffset >= 0x10000)
                Ok("fih.pfs_bounds", $"pfs @0x{fih.PfsImageOffset:X}..0x{imgEnd:X} inside file");
            else
                Fail("fih.pfs_bounds", $"pfs @0x{fih.PfsImageOffset:X} + 0x{fih.PfsImageSize:X} = 0x{imgEnd:X} vs file 0x{sz:X}");
            if ((long)fih.EmbeddedCntOffset > 0 && (long)fih.EmbeddedCntOffset < sz)
                Ok("fih.cnt_bounds", $"embedded CNT @0x{fih.EmbeddedCntOffset:X}");
            else
                Fail("fih.cnt_bounds", $"embedded CNT offset 0x{fih.EmbeddedCntOffset:X} outside file");
            if (fih.NapsLayoutSize > 0) Ok("fih.naps_layout", $"{fih.NapsLayoutSize:N0} bytes");
            else                        Warn("fih.naps_layout", "size = 0 (unusual for a data-first inner)");
        }

        // Signature wrap over CNT
        try
        {
            bool okSig = ProsperoPackageArchive.VerifyCntMetadataSignature(pkg);
            if (okSig) Ok("cnt.wrap", "RSA-3072 public wrap verifies");
            else       Fail("cnt.wrap", "RSA-3072 public wrap DID NOT verify — CNT tampered or wrong keys");
        }
        catch (Exception ex) { Fail("cnt.wrap", "check threw: " + ex.Message); }

        // Required CNT entries
        Step("cnt");
        var tmpCnt = Path.Combine(RealPath(tempRoot ?? Path.GetTempPath()), "fpkg-validate-" + Guid.NewGuid().ToString("N"));
        System.Collections.Generic.List<string> cntFiles = new();
        try
        {
            Directory.CreateDirectory(tmpCnt);
            cntFiles = new System.Collections.Generic.List<string>(
                ProsperoPackageArchive.ExtractCntEntries(pkg, tmpCnt, passcode, includeEncrypted: true));
            var need = new[] { "param.json", "icon0.png" };
            foreach (var n in need)
            {
                var p = Path.Combine(tmpCnt, n);
                if (File.Exists(p)) Ok("cnt." + n, $"{new FileInfo(p).Length:N0} bytes present");
                else                Fail("cnt." + n, "MISSING from CNT — installer will reject");
            }
            // PlayGo prepared set: presence is not enough — a set under the wrong names (JSON text
            // as hash-table.dat, a hash table as ficm.dat) installs but dies at launch with
            // CE-100022-5. Check the on-wire format with the same helpers the build uses.
            foreach (var n in new[] { "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat" })
            {
                var p = Path.Combine(tmpCnt, n);
                if (!File.Exists(p))
                {
                    if (n == "playgo-chunk.dat") Fail("cnt." + n, "MISSING from CNT — installer will reject");
                    else                         Warn("cnt." + n, "absent from CNT (the builder normally emits it)");
                    continue;
                }
                var b = File.ReadAllBytes(p);
                bool ok = n switch
                {
                    "playgo-chunk.dat"      => LooksLikePlayGoChunkDat(b),
                    "playgo-hash-table.dat" => LooksLikePlayGoHashTable(b),
                    _                       => LooksLikePlayGoFicm(b),
                };
                if (ok) Ok("cnt." + n, $"{b.Length:N0} bytes, on-wire format OK");
                else    Fail("cnt." + n, $"{b.Length:N0} bytes but NOT the {n} format" + (LooksLikeJsonText(b) ? " (it is JSON text)" : LooksLikePlayGoHashTable(b) ? " (it is a hash table)" : "") + " — launch will fail (CE-100022-5)");
            }
            {
                var scenP = Path.Combine(tmpCnt, "playgo-scenario.json");
                if (File.Exists(scenP))
                {
                    var sb = File.ReadAllBytes(scenP);
                    if (LooksLikePlayGoScenarioJson(sb)) Ok("cnt.playgo-scenario.json", $"{sb.Length:N0} bytes, valid scenario metadata");
                    else Warn("cnt.playgo-scenario.json", $"{sb.Length:N0} bytes but does not validate as scenario metadata");
                }
            }
            // Whole-set check with the library's own validator: chunk/scenario tables in range,
            // FICM ids inside the chunk table, scenario.json consistent with chunk.dat, and the
            // extents contiguous and covering exactly the package image before the embedded CNT.
            // A set copied from another package fails here (its ranges describe that image).
            {
                string Pg(string n) => Path.Combine(tmpCnt, n);
                if (File.Exists(Pg("playgo-chunk.dat")) && File.Exists(Pg("playgo-hash-table.dat")) && File.Exists(Pg("playgo-ficm.dat")))
                {
                    try
                    {
                        var scen = File.Exists(Pg("playgo-scenario.json")) ? File.ReadAllBytes(Pg("playgo-scenario.json")) : Array.Empty<byte>();
                        ulong? mount = fih != null && (long)fih.EmbeddedCntOffset > 0 ? (ulong)fih.EmbeddedCntOffset : null;
                        var info = ProsperoPlayGo.ValidateLayout(File.ReadAllBytes(Pg("playgo-chunk.dat")), File.ReadAllBytes(Pg("playgo-ficm.dat")),
                                                                 File.ReadAllBytes(Pg("playgo-hash-table.dat")), scen, h.ContentId, mount);
                        Ok("cnt.playgo-layout", $"{info.ChunkCount} chunk(s), {info.ScenarioCount} scenario(s), {info.ExtentCount} extent(s) covering 0x{info.CoveredBytes:X}"
                            + (mount != null ? " = the image before the CNT" : "") + $", {info.FileCount:N0} file(s)");
                    }
                    catch (Exception ex) { Fail("cnt.playgo-layout", ex.Message + " — the set does not describe this image (launch fails with CE-100022-5)"); }
                }
            }
            // param.json coherence with header
            var pj = Path.Combine(tmpCnt, "param.json");
            if (File.Exists(pj))
            {
                try
                {
                    var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(pj));
                    var root = doc.RootElement;
                    if (root.TryGetProperty("contentId", out var cid))
                    {
                        var v = cid.GetString() ?? "";
                        if (v == h.ContentId) Ok("param.contentId", "matches CNT header");
                        else Fail("param.contentId", $"'{v}' != CNT header '{h.ContentId}'");
                    }
                    else Fail("param.contentId", "param.json has no contentId");
                    // The title id is characters 7..15 of the content id (XX0000-TTTTNNNNN_00-...).
                    string headerTid = h.ContentId.Length >= 16 ? h.ContentId.Substring(7, 9) : "";
                    if (root.TryGetProperty("titleId", out var tid))
                    {
                        var v = tid.GetString() ?? "";
                        if (v == headerTid) Ok("param.titleId", $"{v} matches the content id");
                        else Fail("param.titleId", $"'{v}' != content id title '{headerTid}'");
                    }
                    else Fail("param.titleId", "param.json has no titleId");
                    if (root.TryGetProperty("applicationDrmType", out var drm))
                    {
                        var v = drm.GetString() ?? "";
                        if (v is "free" or "standard" or "upgradable") Ok("param.applicationDrmType", $"{v} (header drm_type=0x{h.DrmType:X})");
                        else Warn("param.applicationDrmType", $"'{v}' is not free/standard/upgradable (header drm_type=0x{h.DrmType:X})");
                    }
                    else Warn("param.applicationDrmType", "absent");
                }
                catch (Exception ex) { Fail("param.parse", ex.Message); }
            }
            Step("entries");
            ValidateSystemEntries(tmpCnt, h.ContentId, Ok, Warn, Fail);
        }
        catch (Exception ex) { Fail("cnt.entries", "extract threw: " + ex.Message); }
        finally { try { Directory.Delete(tmpCnt, recursive: true); } catch { } }

        // The inner PFS is read in place: its file table, then eboot.bin alone through the
        // random-access reader. Until 2.2.0 this extracted the whole /app0 into the system
        // temp folder to prove the PFS decodes; a 160 GB game filled the Mac's disk while the
        // caller's bar stood at 0 %.
        Step("inner");
        try
        {
            using var img = new InnerImage(pkg, passcode, cacheBlockSize: 4 << 20, cacheBlocks: 8);
            var files = img.Pfs.GetAllFiles().ToList();
            Ok("inner.pfs", $"{files.Count:N0} file(s) in the inner PFS");
            var eboot = files.FirstOrDefault(f => SafeRelative(img.RelativePath(f)) == "eboot.bin");
            if (eboot == null)
                Fail("inner.eboot", "eboot.bin not present in inner PFS");
            else if (eboot.size < 64)
                Fail("eboot.size", $"{eboot.size} bytes — truncated or empty, cannot start");
            else
            {
                using var ms = new MemoryStream();
                img.CopyFile(eboot, ms);          // the one file the console must start; proves the PFS decodes
                var head = ms.GetBuffer();
                uint magic = ms.Length < 4 ? 0u : (uint)(head[0] | (head[1] << 8) | (head[2] << 16) | (head[3] << 24));
                Ok("inner.decode", $"eboot.bin read through the PFS ({eboot.size:N0} bytes)");
                // 0xEEF51454 ("SCE" fake-self) is what LibProsperoPkg's MakeFself and Sony's SELFs
                // carry; 0x1D3D154F is the PS4 SELF magic and does not load on a PS5.
                if      (magic == 0xEEF51454u) Ok("eboot.magic", "SCE fake-self (0xEEF51454) — good");
                else if (magic == 0x1D3D154Fu) Fail("eboot.magic", "PS4 SELF magic (0x1D3D154F) — not a PS5 executable");
                else if (magic == 0x464C457Fu) Fail("eboot.magic", "raw ELF (0x7F454C46) — not fake-signed; kstuff-fpkg install path will refuse");
                else Warn("eboot.magic", $"unrecognized 0x{magic:X8}");
            }
        }
        catch (Exception ex) { Fail("inner.pfs", "read threw: " + ex.Message); }

        Step("report");
        Report(checks, json);
        int fails = 0;
        foreach (var r in checks) if (r.Level == "fail") fails++;
        return fails == 0 ? 0 : 1;
    }

    /// <summary>Checks on the package's sce_sys metadata entries beyond presence: presentation
    /// images and their DDS, license, NP files, sound, titles, versions. Rules follow the
    /// publishing tools' documented requirements; the retail reference package passes all.</summary>
    static void ValidateSystemEntries(string cnt, string contentId, Action<string, string> Ok, Action<string, string> Warn, Action<string, string> Fail)
    {
        string P(string n) => Path.Combine(cnt, n);

        // Presentation images: 8-bit, non-interlaced, RGB (icon0/pic0/pic1) or RGBA (pic2);
        // icon0 512x512 with a matching BC7 icon0.dds (without it the package never starts).
        foreach (var f in Directory.EnumerateFiles(cnt, "*.png").OrderBy(x => x, StringComparer.Ordinal))
        {
            var n = Path.GetFileName(f);
            if (PresentationMedia.Kind(n) == null) continue;
            var info = PresentationMedia.ReadPng(f);
            var problem = PresentationMedia.Problem(n, info);
            bool icon = PresentationMedia.Kind(n) == "icon0";
            if (problem == null) Ok("image." + n, $"{info!.Value.Width}x{info.Value.Height} 8-bit {(info.Value.Colour == 2 ? "RGB" : "RGBA")}");
            else if (icon && info == null) Fail("image." + n, "not a PNG — the console cannot show or start the title");
            else Warn("image." + n, problem);
            if (PresentationMedia.SizeNote(n, info) is string note) Warn("image." + n + ".size", note);
            var ddsName = Path.ChangeExtension(n, ".dds");
            if (!EntryNames.NameToId.ContainsKey(ddsName)) continue;
            var ddsWhy = File.Exists(P(ddsName)) ? PresentationMedia.DdsProblem(P(ddsName), info) : "missing";
            if (ddsWhy == null) Ok("image." + ddsName, "DX10 BC7, same size as the PNG");
            else if (icon) Fail("image." + ddsName, ddsWhy + " — the package installs but will not start");
            else Warn("image." + ddsName, ddsWhy);
        }

        // License: a standard/upgradable title needs both entries, for this content id.
        string drm = "";
        JsonElement root = default;
        bool haveParam = false;
        try
        {
            using var d = JsonDocument.Parse(File.ReadAllBytes(P("param.json")));
            root = d.RootElement.Clone();
            haveParam = root.ValueKind == JsonValueKind.Object;
            if (haveParam && root.TryGetProperty("applicationDrmType", out var dt)) drm = dt.GetString() ?? "";
        }
        catch (Exception ex) when (ex is IOException or JsonException) { }
        bool needLicense = !string.Equals(drm, "free", StringComparison.OrdinalIgnoreCase);
        foreach (var n in new[] { "license.dat", "license.info" })
        {
            if (!File.Exists(P(n)))
            {
                if (needLicense) Fail("license." + n, $"missing (applicationDrmType '{drm}') — the title shows a padlock and will not start");
                else Ok("license." + n, "not needed (free)");
                continue;
            }
            var data = File.ReadAllBytes(P(n));
            string? err;
            bool ok = n == "license.dat" ? ProsperoSystemFiles.ValidateLicenseDat(data, contentId, out err)
                                         : ProsperoSystemFiles.ValidateLicenseInfo(data, contentId, out err);
            if (ok) Ok("license." + n, $"{data.Length:N0} bytes, for {contentId}");
            else Fail("license." + n, err ?? "invalid");
        }

        // NP files (trophies, activities): structure as the builder itself validates it.
        foreach (var n in new[] { "nptitle.dat", "npbind.dat", "trophy2/npbind.dat", "uds/npbind.dat" })
        {
            if (!File.Exists(P(n))) continue;
            var data = File.ReadAllBytes(P(n));
            string? err, id;
            bool ok = n == "nptitle.dat" ? ProsperoSystemFiles.ValidateNptitle(data, out id, out err)
                                         : ProsperoSystemFiles.ValidateNpbind(data, out id, out err);
            if (ok) Ok("np." + n, id != null ? $"id {id}" : "valid");
            else Warn("np." + n, (err ?? "invalid") + " — trophies or activities may not work");
        }

        // snd0.at9: a RIFF/WAVE file at 48 kHz.
        if (File.Exists(P("snd0.at9")))
        {
            var b = File.ReadAllBytes(P("snd0.at9"));
            string? why = null;
            if (b.Length < 12 || b[0] != 'R' || b[1] != 'I' || b[2] != 'F' || b[3] != 'F' || b[8] != 'W' || b[9] != 'A' || b[10] != 'V' || b[11] != 'E')
                why = "not a RIFF/WAVE file";
            else
            {
                int o = 12; uint rate = 0;
                while (o + 8 <= b.Length)
                {
                    uint size = U32(b, o + 4);
                    if (b[o] == 'f' && b[o + 1] == 'm' && b[o + 2] == 't' && b[o + 3] == ' ' && o + 16 <= b.Length) { rate = U32(b, o + 12); break; }
                    o += 8 + (int)Math.Min(size + (size & 1), int.MaxValue - 8);
                    if (o <= 0) break;
                }
                if (rate != 48000) why = rate == 0 ? "no fmt chunk" : $"{rate} Hz, needs 48000";
            }
            if (why == null) Ok("sound.snd0.at9", "RIFF/WAVE, 48 kHz");
            else Warn("sound.snd0.at9", why + " — the preview sound may not play");
        }

        if (!haveParam) return;
        // Titles: the default language exists and has a clean, non-empty titleName.
        if (root.TryGetProperty("localizedParameters", out var lp) && lp.ValueKind == JsonValueKind.Object
            && lp.TryGetProperty("defaultLanguage", out var dl) && dl.GetString() is string lang
            && lp.TryGetProperty(lang, out var block) && block.ValueKind == JsonValueKind.Object
            && block.TryGetProperty("titleName", out var tn) && tn.GetString() is string title)
        {
            if (title.Length == 0) Warn("param.title", $"{lang} titleName is empty");
            else if (title != title.Trim() || title.Any(char.IsControl) || title.Contains('\uFEFF'))
                Warn("param.title", $"{lang} titleName has leading/trailing spaces or control characters");
            else Ok("param.title", $"{lang}: {title}");
        }
        else Warn("param.title", "no titleName for the default language");

        // Versions: contentVersion xx.xxx.xxx, masterVersion xx.xx.
        if (root.TryGetProperty("contentVersion", out var cv) && cv.GetString() is string cvs)
        {
            if (System.Text.RegularExpressions.Regex.IsMatch(cvs, @"^\d{2}\.\d{3}\.\d{3}$")) Ok("param.contentVersion", cvs);
            else Warn("param.contentVersion", $"'{cvs}' is not xx.xxx.xxx");
        }
        if (root.TryGetProperty("masterVersion", out var mv) && mv.GetString() is string mvs
            && !System.Text.RegularExpressions.Regex.IsMatch(mvs, @"^\d{2}\.\d{2}$"))
            Warn("param.masterVersion", $"'{mvs}' is not xx.xx");

        // System software the title asks for (the builder caps it at 9.00 and floors it at the
        // SDK version; a console below it refuses to start the title).
        if (root.TryGetProperty("requiredSystemSoftwareVersion", out var rs) && rs.GetString() is string rss)
        {
            var fw = FirmwareText(rss);
            var sdk = root.TryGetProperty("sdkVersion", out var sv) && sv.GetString() is string svs ? FirmwareText(svs) : null;
            if (fw == null) Warn("param.firmware", $"requiredSystemSoftwareVersion '{rss}' is not 0x + 16 hex digits");
            else Ok("param.firmware", $"requires system software {fw}" + (sdk != null ? $" (SDK {sdk})" : ""));
        }
    }

    /// <summary>"0x0910000000000000" → "9.10"; null if not a 16-digit hex version.</summary>
    internal static string? FirmwareText(string v)
    {
        if (v.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) v = v[2..];
        if (v.Length != 16 || !v.All(Uri.IsHexDigit)) return null;
        return $"{Convert.ToInt32(v[..2], 16):X}.{v.Substring(2, 2)}";
    }

    internal sealed class ValidateResult
    {
        public string Check { get; set; } = "";
        public string Level { get; set; } = "pass"; // pass / warn / fail
        public string Message { get; set; } = "";
    }

    static void Report(System.Collections.Generic.List<ValidateResult> checks, bool json)
    {
        int p = 0, w = 0, f = 0;
        foreach (var r in checks) { if (r.Level == "pass") p++; else if (r.Level == "warn") w++; else f++; }
        if (json)
        {
            Console.WriteLine(JsonSerializer.Serialize(new ValidateReportDoc
            {
                pass = p, warn = w, fail = f,
                results = checks,
            }, PkgToolJsonContext.Indented.ValidateReportDoc));
            return;
        }
        foreach (var r in checks)
        {
            string tag = r.Level switch { "pass" => "[ok  ]", "warn" => "[WARN]", _ => "[FAIL]" };
            Console.WriteLine($"  {tag}  {r.Check,-24}  {r.Message}");
        }
        Console.WriteLine();
        Console.WriteLine($"summary: {p} passed, {w} warned, {f} failed");
    }

    /// <summary>True when <paramref name="path"/> is <paramref name="root"/> or lies below it.</summary>
    static bool IsInside(string path, string root)
    {
        var p = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        var r = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        return p.Equals(r, StringComparison.Ordinal) || p.StartsWith(r + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }

    /// <summary>Copy *src* into *dst*: real subdirectories and a real copy of every regular file
    /// (clutter left out, see FsJunk), modification times kept. Prints "[stage] copy NN% (X of
    /// Y GB)" about once a second for the GUI's bar. Never hard links: through a link every
    /// pre-build change (fake-signing, dropped PlayGo files, the rewritten param.json) would
    /// reach the caller's source.</summary>
    static void MirrorSource(string src, string dst, long totalBytes)
    {
        long done = 0, lastTick = Environment.TickCount64 - 1000;
        void Report(bool force)
        {
            if (!force && Environment.TickCount64 - lastTick < 1000) return;
            lastTick = Environment.TickCount64;
            int pct = totalBytes > 0 ? (int)Math.Min(100, done * 100 / totalBytes) : 100;
            Console.Error.WriteLine($"[stage] copy {pct}% ({done / 1073741824.0:F1} of {totalBytes / 1073741824.0:F1} GB)");
        }
        var buffer = new byte[4 * 1024 * 1024];
        foreach (var d in Directory.EnumerateDirectories(src, "*", SearchOption.AllDirectories))
        {
            var relDir = Path.GetRelativePath(src, d);
            if (FsJunk.PathHasJunk(relDir)) continue;
            Directory.CreateDirectory(Path.Combine(dst, relDir));
        }
        foreach (var f in Directory.EnumerateFiles(src, "*", SearchOption.AllDirectories))
        {
            var relFile = Path.GetRelativePath(src, f);
            if (FsJunk.PathHasJunk(relFile)) continue;
            var target = Path.Combine(dst, relFile);
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            if (File.Exists(target) || IsSymlink(target)) File.Delete(target);
            using (var input = new FileStream(f, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20))
            using (var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 20))
            {
                int n;
                while ((n = input.Read(buffer, 0, buffer.Length)) > 0)
                {
                    output.Write(buffer, 0, n);
                    done += n;
                    Report(false);
                }
            }
            try { File.SetLastWriteTimeUtc(target, File.GetLastWriteTimeUtc(f)); } catch { }
        }
        Report(true);
    }

    static long DirectorySize(string root)
    {
        long total = 0;
        try { foreach (var f in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories)) { try { total += new FileInfo(f).Length; } catch { } } } catch { }
        return total;
    }


    static bool IsSymlink(string p)
    {
        try { return File.Exists(p) && new FileInfo(p).LinkTarget != null; } catch { return false; }
    }

    /// <summary>Builds up to 1.1.17 unpacked their native library into
    /// $DOTNET_BUNDLE_EXTRACT_BASE_DIR (default ~/.net)/ffpfsc-pkg-tool/&lt;bundle hash&gt;/ — one
    /// folder of about 27 MB per build, never removed. This build unpacks nothing, so the whole
    /// folder is ours to delete. Skipped while another copy of the tool runs: that may be an
    /// older build still loading from its folder.</summary>
    static void RemoveOldBundleExtractions()
    {
        bool trace = Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1";
        try
        {
            var baseRoot = Environment.GetEnvironmentVariable("DOTNET_BUNDLE_EXTRACT_BASE_DIR");
            if (string.IsNullOrEmpty(baseRoot))
                baseRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".net");
            var root = Path.Combine(baseRoot, "ffpfsc-pkg-tool");
            if (!Directory.Exists(root)) return;
            using var self = System.Diagnostics.Process.GetCurrentProcess();
            int others = 0;
            foreach (var p in System.Diagnostics.Process.GetProcessesByName(self.ProcessName))
                using (p) if (p.Id != self.Id) others++;
            if (others > 0)
            {
                if (trace) Console.Error.WriteLine($"[trace] old bundle folders kept: {others} other tool process(es) running");
                return;
            }
            Directory.Delete(root, recursive: true);
            if (trace) Console.Error.WriteLine("[trace] removed old bundle folders under " + root);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            if (trace) Console.Error.WriteLine("[trace] could not remove old bundle folders: " + ex.Message);
        }
    }

    /// <summary>Direct Magick.NET's DllImport lookups for <c>Magick.Native-Q8-arm64.dll</c>
    /// at the runtimes/osx-arm64/native/ file the runtime extracts alongside the exe.
    /// Magick's P/Invoke uses the bare "Magick.Native-Q8-arm64.dll" name (Windows-style),
    /// which .NET on macOS maps to Magick.Native-Q8-arm64.dll.dylib — but only when the
    /// file lives on the default probing path, which a single-file exe does not set up for
    /// it. We resolve it once, ourselves, from the executable's own directory.</summary>
    static bool _magickRedirected;
    static void RedirectMagickNative()
    {
        if (_magickRedirected) return;
        _magickRedirected = true;
        bool trace = Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1";
        var baseDir = AppContext.BaseDirectory ?? Environment.CurrentDirectory;
        // The ImageMagick library ships next to the executable (IncludeNativeLibrariesFor
        // SelfExtract=false, so nothing is unpacked at run time). Probe only this build's own
        // directory — the executable's real location and the base directory — plus their
        // runtimes/<RID>/native/ (the NuGet layout of a framework-dependent run). Never the
        // folders older builds unpacked under ~/.net, and never $TMPDIR.
        var exeDir = Environment.ProcessPath is string pp
            ? Path.GetDirectoryName(new FileInfo(pp).ResolveLinkTarget(returnFinalTarget: true)?.FullName ?? pp)
            : null;
        string[] Candidates()
        {
            var acc = new List<string>();
            void AddRoot(string? root)
            {
                if (string.IsNullOrEmpty(root) || !Directory.Exists(root) || acc.Contains(root)) return;
                acc.Add(root);
                var runtimes = Path.Combine(root, "runtimes");
                if (!Directory.Exists(runtimes)) return;
                try
                {
                    foreach (var rid in Directory.EnumerateDirectories(runtimes))
                    {
                        var native = Path.Combine(rid, "native");
                        if (Directory.Exists(native)) acc.Add(native);
                    }
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            AddRoot(exeDir);
            AddRoot(baseDir);
            return acc.ToArray();
        }
        string[] candidates = Candidates();
        if (trace) Console.Error.WriteLine("[trace] Magick resolver: baseDir=" + baseDir + "  candidates=" + string.Join(":", candidates));
        // Every Magick assembly has its own DllImport stubs — register the resolver on
        // ALL of them (Magick.NET, Magick.NET.Core, and any *.NativeInteropGenerator
        // source-generated assemblies).
        int hooked = 0;
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            var n = asm.GetName().Name ?? "";
            if (!n.StartsWith("Magick", StringComparison.OrdinalIgnoreCase)) continue;
            try { NativeLibrary.SetDllImportResolver(asm, MagickResolver); hooked++; if (trace) Console.Error.WriteLine("[trace] Magick resolver hooked on: " + n); }
            catch (Exception ex) { if (trace) Console.Error.WriteLine("[trace] Magick resolver skip " + n + ": " + ex.Message); }
        }
        // Hook every assembly loaded later, too — Magick.NET has multiple.
        AppDomain.CurrentDomain.AssemblyLoad += (_, e) =>
        {
            var n = e.LoadedAssembly.GetName().Name ?? "";
            if (n.StartsWith("Magick", StringComparison.OrdinalIgnoreCase))
                try { NativeLibrary.SetDllImportResolver(e.LoadedAssembly, MagickResolver); if (trace) Console.Error.WriteLine("[trace] Magick resolver hooked on late: " + n); }
                catch { }
        };
        if (trace) Console.Error.WriteLine("[trace] Magick resolver initial hooks: " + hooked);

        IntPtr MagickResolver(string name, Assembly _, DllImportSearchPath? __)
        {
            if (!name.StartsWith("Magick.Native", StringComparison.OrdinalIgnoreCase)) return IntPtr.Zero;
            var probe = candidates;
            if (trace) Console.Error.WriteLine("[trace] Magick resolver: probing " + probe.Length + " dirs for " + name);
            foreach (var dir in probe)
                foreach (var suffix in new[] { ".dylib", ".dll.dylib", "" })
                {
                    var path = Path.Combine(dir, name + suffix);
                    if (File.Exists(path))
                    {
                        try { var h = NativeLibrary.Load(path); if (trace) Console.Error.WriteLine("[trace] Magick resolver: loaded " + path); return h; }
                        catch (Exception ex) { if (trace) Console.Error.WriteLine("[trace] Magick resolver: dlopen failed " + path + ": " + ex.Message); }
                    }
                }
            if (trace) Console.Error.WriteLine("[trace] Magick resolver: no candidate for " + name);
            return IntPtr.Zero;
        }
    }

        static int CmdBuild(string[] args)
    {
        if (args.Length < 3) return Bad("build needs <src-dir> <out-dir>");
        bool autoFakeSign = true;         // fake-sign raw ELFs in source before build (idempotent)
        bool playGoChunksExplicit = false; // did the user pass --playgo-chunks?
        bool retailNormalize = true;      // auto-upgrade a "standard" retail source to a shape the console launches
        bool regenPlayGo = false;         // force-discard a prepared PlayGo set even if it validates
        bool amprIndex = true;            // rebuild ampr_emu.index when the AMPR emulator is shipped
        string hdrFlag = "auto";          // param.json attribute bit 29 (HDR support): auto = as the source declares, on, off

        var opts = new ProsperoBuildOptions
        {
            SourceFolder = args[1],
            OutputFolder = args[2],
            Mode = ProsperoPackageMode.Application,
            OutputFormat = ProsperoOutputFormat.DebugImage,
            InnerCompression = ProsperoInnerCompression.None,
            KrakenBackend = ProsperoKrakenBackend.BuiltIn,
            Passcode = new string('0', 32),
            Version = "01.000.000",
            // Workers: 0 = the library's automatic (one per core). One knob drives both the
            // inner-image Kraken workers and the outer-PFS Parallel.For. The crashes that once
            // pinned this to 1 (AccessViolationException on thread-pool workers, 4 of 6 runs
            // with -j 4) were the compressed single-file bundle, not the encoder: with
            // EnableCompressionInSingleFile off (PkgTool.csproj) 4, 8 and 12 workers ran 22 of
            // 22 builds byte-identical to the single-worker package. --parallelism / -j sets it.
            KrakenMaxDegreeOfParallelism = 0,
            LegacyZlibMaxDegreeOfParallelism = 0,
            // PS5 debug-image loader only accepts the plaintext-no-auth outer PFS
            // (mode 0x000D with the "PPPLAIN-NOAUTH!" seed marker) — a random-seed
            // AES-XTS wrap validates structurally and extract-inner reads it fine,
            // but the console-loader rejects it with CE-100096-6 at launch time
            // (verified on FW 11.60 + kstuff-1.13-dr-test3, retail PS5, 2026-09-24).
            // Sony's Publishing Tools DLL always uses this mode for debug images;
            // a diff of pfs-dump showed the inner PFS is byte-identical to a
            // reference build made with libScePubTools.dll — only the outer PFS
            // wrap differed. Setting PlaintextNoAuth makes our output byte-exact.
            PublisherImageMode = ProsperoPublisherImageMode.PlaintextNoAuth,
            // Sony's publisher packs every launch-time file into PlayGo chunk 0;
            // LibProsperoPkg's default of 64 spreads them out. Both boot, but the
            // 1-chunk layout matches every known-working reference package. Auto-
            // detected from the source's own sce_sys/playgo-chunk.dat later.
            PlayGoChunkCount = 1,
            // A source prepared set is never copied into the package: the library keeps its
            // file assignments, chunk labels, languages and scenario presentation and
            // rebuilds the image ranges for the image it just made.
            PreserveSourcePlayGoLayout = true,
            // Inner-image layout pass (the library's default, "matches the Publishing Tools
            // layout pass"). It rewrites the image only when that saves >= 1 MiB and
            // 0.1 %; on the console-verified retail sample (2026-10-05) it found 512 KiB
            // and left the layout alone. --layout-pass off keeps the 1.1.x layout.
            EnableOuterBlockCoalescing = true,
            EnableRelocationAlignmentAdjustment = true,
        };
        bool stageInPlace = false;   // the source is the caller's own working copy: no mirror
        bool consumeSource = false;  // with it: delete each source file once the inner image holds it
        for (int i = 3; i < args.Length; i++)
        {
            string a = args[i];
            switch (a)
            {
                case "--stage-in-place":
                    // The source is the caller's own working copy (an image it unpacked into
                    // its scratch): the pre-build changes go straight into it, nothing is copied.
                    stageInPlace = true;
                    break;
                case "--consume-source":
                    // Only with --stage-in-place: the moment the library reports a file packed
                    // into the inner image, that source file is deleted, so the unpacked game
                    // shrinks while the image grows. The caller unpacks again after a failure.
                    consumeSource = true;
                    break;
                case "--content-id": opts.ContentId = Need(args, ref i, a); break;
                case "--title-id": opts.TitleId = Need(args, ref i, a); break;
                case "--title": opts.Title = Need(args, ref i, a); break;
                case "--version": opts.Version = Need(args, ref i, a); break;
                case "--passcode": opts.Passcode = Need(args, ref i, a); break;
                case "--mode":
                {
                    var v = Need(args, ref i, a);
                    opts.InnerCompression = v.ToLowerInvariant() switch
                    {
                        "none" => ProsperoInnerCompression.None,
                        "zlib" => ProsperoInnerCompression.Zlib,
                        "kraken" => ProsperoInnerCompression.Kraken,
                        _ => throw new ArgumentException($"unknown --mode: {v}")
                    };
                    break;
                }
                case "--kraken-backend":
                {
                    var v = Need(args, ref i, a);
                    opts.KrakenBackend = v.ToLowerInvariant() switch
                    {
                        "automatic" => ProsperoKrakenBackend.Automatic,
                        "builtin" => ProsperoKrakenBackend.BuiltIn,
                        "publishingtools" => ProsperoKrakenBackend.PublishingToolsRequired,
                        "uncompressed" => ProsperoKrakenBackend.Uncompressed,
                        _ => throw new ArgumentException($"unknown --kraken-backend: {v}")
                    };
                    if (opts.KrakenBackend is ProsperoKrakenBackend.Uncompressed or ProsperoKrakenBackend.Automatic)
                        // Verified on a retail PS5 (FW 11.60, kstuff-lite 1.13): the stored/
                        // automatic path installs and shows its icon but the launch fails with
                        // CE-100096-6. Only the built-in Kraken encoder is console-launchable.
                        Console.Error.WriteLine($"[warn] --kraken-backend {v}: this output is NOT launchable on a console (CE-100096-6 verified); use 'builtin'");
                    break;
                }
                case "--pubtools-dll": opts.PublishingToolsLibraryPath = Need(args, ref i, a); break;
                case "--parallelism":
                case "-j":
                {
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int j) || j < 0) throw new ArgumentException("--parallelism needs an integer >= 0 (0 = one worker per core)");
                    opts.KrakenMaxDegreeOfParallelism = j;
                    opts.LegacyZlibMaxDegreeOfParallelism = j;
                    break;
                }
                case "--deterministic": opts.DeterministicBuild = true; break;
                case "--temp":
                {
                    var v = Need(args, ref i, a);
                    if (string.IsNullOrWhiteSpace(v)) throw new ArgumentException("--temp needs a directory");
                    Directory.CreateDirectory(v);
                    opts.TemporaryDirectory = v;
                    break;
                }
                case "--level":
                {
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int lvl)) throw new ArgumentException("--level needs an integer");
                    // Kraken accepts -4..9 (drakmor's encoder), the legacy zlib path 0..9.
                    opts.KrakenCompressionLevel = Math.Clamp(lvl, -4, 9);
                    opts.LegacyZlibCompressionLevel = Math.Clamp(lvl, 0, 9);
                    break;
                }
                case "--playgo-chunks":
                {
                    // The count the builder uses when it generates the set (no usable source
                    // set). LibProsperoPkg defaults to 100 language chunks; Sony's publisher
                    // packs every launch-time file into chunk 0 (default here). A source
                    // playgo-chunk.dat overrides both. The library rejects anything outside 1..255.
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int chunks) || chunks < 1 || chunks > 255)
                        throw new ArgumentException("--playgo-chunks needs an integer 1..255 (LibProsperoPkg's range)");
                    opts.PlayGoChunkCount = chunks;
                    playGoChunksExplicit = true;
                    break;
                }
                case "--no-fake-sign":
                    // Retail games have raw ELFs that must be fake-signed to boot on a
                    // jailbroken console. Default is ON so a retail-shaped source (with
                    // eboot.bin + fakelib SPRXes) becomes launch-ready without a separate
                    // Sign pass. Use this flag to skip if the source is already fake-signed
                    // and you want to keep bytes identical, or if signing would corrupt an
                    // exotic SELF variant.
                    autoFakeSign = false;
                    break;
                case "--fake-sign":
                    // Explicit opt-in (redundant with the default). Kept for clarity.
                    autoFakeSign = true;
                    break;
                case "--no-retail-normalize":
                    // Retail-normalize replaces placeholder license.dat/info from the source
                    // with a valid debug license issued by LibProsperoPkg (fixes "bad RIF
                    // magic" skip) and stamps the retail SELF pattern; applicationDrmType
                    // stays as the source declares it (the library writes drm_type=16 for
                    // every non-free Application). Turn OFF for byte-exact re-packs or when
                    // packing something already finalized by Sony's tools.
                    retailNormalize = false;
                    break;
                case "--retail-normalize":
                    retailNormalize = true;
                    break;
                case "--layout-pass":
                {
                    var v = Need(args, ref i, a).ToLowerInvariant();
                    bool on = v switch { "on" => true, "off" => false, _ => throw new ArgumentException("--layout-pass needs on or off") };
                    opts.EnableOuterBlockCoalescing = on;
                    opts.EnableRelocationAlignmentAdjustment = on;
                    break;
                }
                case "--regen-playgo":
                    // Discard the source's sce_sys/playgo-*.dat even when they validate, so
                    // LibProsperoPkg generates a set that matches the inner tree it builds.
                    regenPlayGo = true;
                    break;
                case "--no-ampr-index":
                    // Keep whatever ampr_emu.index the source ships (or none) instead of
                    // rebuilding it over the files that are actually packed.
                    amprIndex = false;
                    break;
                case "--hdr-flag":
                    // auto: keep the source's declaration (a console on "HDR when supported"
                    // follows bit 29 — set means it switches to HDR output for this title).
                    // on: set the bit even if the source lacks it. off: clear it.
                    hdrFlag = Need(args, ref i, a).ToLowerInvariant();
                    if (hdrFlag is not ("auto" or "on" or "off"))
                        throw new ArgumentException("--hdr-flag needs auto, on or off");
                    break;
                case "--no-hdr-flag": hdrFlag = "off"; break;   // 1.1.12/1.1.13 spelling
                default: return Bad("unknown build flag: " + a);
            }
        }
        if (string.IsNullOrWhiteSpace(opts.ContentId)) return Bad("--content-id required");
        if (string.IsNullOrWhiteSpace(opts.TitleId)) return Bad("--title-id required");
        Directory.CreateDirectory(opts.OutputFolder);

        // Stage: every build works on a stage, a real copy of the source, or the source itself
        // when the caller says it is its own working copy (--stage-in-place). Never hard links.
        // The pre-build transforms run on that stage:
        //   (a) auto-generate sce_sys/*.dds from PNGs the source only provides as PNG
        //       (LibProsperoPkg's builder moves sce_sys/*.dds into the outer CNT but does
        //       NOT generate the DDS itself; without them the .pkg installs but never
        //       launches — see the 1.1.7 CHANGELOG entry);
        //   (b) fake-sign raw ELFs (eboot.bin, *.elf, *.prx, *.sprx) that a retail-shape
        //       source ships unsigned — a jailbroken PS5's app loader rejects a package
        //       whose eboot is not a fake-self and kills the process immediately (short
        //       fan-spin then CE-100096-6). Idempotent — already-signed inputs are skipped.
        // Staging is unconditional because LibProsperoPkg's EnsureParamJson writes a generated
        // sce_sys/param.json into the folder it builds from when none exists — which used to
        // be the user's source folder whenever nothing else needed staging.
        // Auto-detect PlayGoChunkCount from the source's own sce_sys/playgo-chunk.dat is a
        // pure metadata read.
        string effectiveSource = opts.SourceFolder!;
        string? autoStage = null;
        var srcSceSys = Path.Combine(effectiveSource, "sce_sys");

        // -- PlayGoChunkCount auto-detect (unless the user pinned it explicitly) --
        bool playgoOutOfRange = false;   // the source's set declares a count the library rejects
        int detectedPlayGoChunks = 0;    // chunk count the source declares (for messages)
        if (!playGoChunksExplicit)
        {
            try
            {
                var pgChunk = Path.Combine(srcSceSys, "playgo-chunk.dat");
                if (File.Exists(pgChunk))
                {
                    var bytes = File.ReadAllBytes(pgChunk);
                    // Layout probed against known good sources (Sony DLL builds):
                    //   0x00  magic 'plgx'
                    //   0x08  u16 attribute count
                    //   0x0A  u16 chunk count  <-- this is what LibProsperoPkg uses
                    if (bytes.Length >= 12 &&
                        bytes[0] == (byte)'p' && bytes[1] == (byte)'l' && bytes[2] == (byte)'g' && bytes[3] == (byte)'x')
                    {
                        int detected = bytes[0x0A] | (bytes[0x0B] << 8);
                        detectedPlayGoChunks = detected;
                        if (detected == 0 || detected > 255)
                        {
                            Console.Error.WriteLine($"  [playgo] source declares {detected} chunks (out of range 1..255): regenerating the prepared set");
                            playgoOutOfRange = true;
                        }
                        else if (detected != opts.PlayGoChunkCount)
                        {
                            Console.Error.WriteLine($"  [playgo] source declares {detected} chunk(s); using that as the generation count too (was default {opts.PlayGoChunkCount})");
                            opts.PlayGoChunkCount = detected;
                        }
                    }
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[warn] could not read source playgo-chunk.dat ({ex.GetType().Name}); using PlayGoChunkCount={opts.PlayGoChunkCount}"); }
        }

        // -- Retail-normalize discovery (read source param.json once) --
        // A "standard"-DRM retail dump needs four things LibProsperoPkg 1.2.0 does not do
        // on its own before a JB PS5 (FW 11.60, kstuff-lite 1.13+) launches the result.
        // All are applied in the staged mirror; the on-disk source is never modified.
        // Verified 2026-09-25 on a retail PS5 with a retail title (A/B builds L vs M):
        //   - drm_type=16 in the CNT header. Upstream hard-codes 0 for any Application
        //     whose applicationDrmType is not "upgradable"; retail packages carry 16.
        //     Fixed upstream of this code by the IL patch in patches/ (no param.json
        //     change needed, so applicationDrmType stays "standard" like Sony's own).
        //   - Valid license.dat/license.info CNT entries (0x0400/0x0401). Dumps ship
        //     placeholder bytes that upstream validates and silently drops; without the
        //     entries the homescreen shows a padlock. We drop the placeholders and hand
        //     upstream a DebugLicenseProvider (its own BuildDebugLicense bytes).
        //   - The retail SELF flavour on eboot.bin and every prx/sprx (ProgramType bit
        //     0x10000000, byte 0x0B of the SELF header). Sony-signed retail SELFs carry
        //     it; older fake-signers used the dev flavour.
        //   - param.json attribute bit 29 (0x20000000) = HDR support flag. A console on
        //     "HDR when supported" switches to HDR output when it is set (verified: the
        //     build with it ran the console in HDR10, the build without in SDR). The
        //     source's own declaration is the publisher's intent, so --hdr-flag auto
        //     (default) keeps it; on/off override. Not launch-critical either way.
        //   Also injected: a "kernel" block with a retail reference package's values when the source has none.
        //   Verified NOT launch-critical (build M had none and launched); kept because
        //   every Sony retail package carries one and L is the validated configuration.
        // Everything the loader actually rejected the launch on turned out to be the
        // PlayGo prepared set (see the sanity block above) — not any of these.
        bool sourceIsStandardApp = false;
        try
        {
            var srcParam = Path.Combine(srcSceSys, "param.json");
            if (File.Exists(srcParam))
            {
                using var doc = JsonDocument.Parse(File.ReadAllBytes(srcParam));
                if (doc.RootElement.TryGetProperty("applicationDrmType", out var dt))
                    sourceIsStandardApp = string.Equals(dt.GetString(), "standard", StringComparison.OrdinalIgnoreCase);
            }
        }
        catch (Exception ex) { Console.Error.WriteLine($"[warn] could not read source param.json for retail-normalize check ({ex.GetType().Name})"); }
        bool doRetailNormalize = retailNormalize && sourceIsStandardApp;

        try
        {
            // Discover work first (so we can stage exactly once).
            var elfsToSign = autoFakeSign ? FindRawElfs(effectiveSource) : Array.Empty<string>();

            // -- PlayGo prepared-set sanity --
            // LibProsperoPkg only COUNTS sce_sys/playgo-{chunk,hash-table,ficm}.dat: 3/3
            // present → "complete prepared set found; its layout will be preserved" and the
            // bytes go verbatim into CNT entries 0x1001/0x2010/0x2011, which ShellCore and
            // the console parses at install/launch. Some containers carry these under the
            // WRONG names (seen in practice: hash-table.dat = param.json text, ficm.dat =
            // the real hash table, origin-param.json = a DDS). Such a package installs
            // and shows its icon but the launch dies with CE-100022-5, while the same
            // content runs from a mounted image because nothing reads those files
            // there. Validate each file against the on-wire format LibProsperoPkg itself
            // emits (ProsperoPlayGo.BuildChunkDat/BuildHashTable/BuildFicm); if any present
            // file fails, drop the whole set from the staged mirror so the builder
            // regenerates a consistent one from the actual inner tree.
            var playgoCoreNames = new[] { "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat" };
            var playgoAllNames = new[] { "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat", "playgo-scenario.json" };
            var playgoPresent = Directory.Exists(srcSceSys)
                ? playgoAllNames.Where(n => File.Exists(Path.Combine(srcSceSys, n))).ToArray()
                : Array.Empty<string>();
            bool hasScenarioJson = playgoPresent.Contains("playgo-scenario.json");
            var playgoBad = new List<string>();
            foreach (var n in playgoPresent)
            {
                byte[] b;
                try { b = File.ReadAllBytes(Path.Combine(srcSceSys, n)); } catch { playgoBad.Add(n + " (unreadable)"); continue; }
                bool ok = n switch
                {
                    "playgo-chunk.dat"      => LooksLikePlayGoChunkDat(b),
                    "playgo-hash-table.dat" => LooksLikePlayGoHashTable(b),
                    "playgo-ficm.dat"       => LooksLikePlayGoFicm(b),
                    "playgo-scenario.json"  => LooksLikePlayGoScenarioJson(b),
                    _                       => false,
                };
                if (!ok) playgoBad.Add(n + (n == "playgo-scenario.json" ? " (not valid scenario JSON)" : LooksLikeJsonText(b) ? " (is JSON text)" : LooksLikePlayGoHashTable(b) ? " (is a hash table)" : " (bad format)"));
            }
            bool discardPlayGo = playgoPresent.Intersect(playgoCoreNames).Any() && (regenPlayGo || playgoBad.Count > 0 || playgoOutOfRange);
            if (discardPlayGo)
                Console.Error.WriteLine(playgoBad.Count > 0
                    ? $"  [playgo] prepared set is CORRUPT — {string.Join(", ", playgoBad)}; discarding all {playgoPresent.Length} file(s) so LibProsperoPkg regenerates a consistent {opts.PlayGoChunkCount}-chunk set"
                    : regenPlayGo
                    ? $"  [playgo] --regen-playgo: discarding prepared set ({playgoPresent.Length} file(s)); LibProsperoPkg will regenerate {opts.PlayGoChunkCount} chunk(s)"
                    : $"  [playgo] discarding prepared set ({playgoPresent.Length} file(s)); LibProsperoPkg will regenerate {opts.PlayGoChunkCount} chunk(s)");

            // Any sce_sys/*.json that is not JSON is a mislabeled dump artifact (same
            // shifted-name bug); it would land in the inner PFS as junk. Drop it.
            var badJson = Directory.Exists(srcSceSys)
                ? Directory.EnumerateFiles(srcSceSys, "*.json").Where(p =>
                    { try { return !LooksLikeJsonText(File.ReadAllBytes(p)); } catch { return false; } })
                  .Select(p => Path.GetFileName(p)!).ToArray()
                : Array.Empty<string>();
            // param.json is the title's identity and settings (attribute, kernel, age level,
            // titles). Dropping a broken one would make the builder write a minimal generic one
            // and silently lose all of that, so refuse instead.
            if (badJson.Contains("param.json", StringComparer.OrdinalIgnoreCase))
                throw new InvalidDataException("sce_sys/param.json is not valid JSON (a damaged or mislabeled dump). "
                    + "A package built now would carry a generic param.json and lose the title's settings. Put the title's "
                    + "real param.json in place, or delete the file to build with a generated one.");
            if (badJson.Length > 0)
                Console.Error.WriteLine($"  [sce_sys] dropping mislabeled non-JSON file(s): {string.Join(", ", badJson)}");

            // Executables: an empty eboot.bin cannot start; an empty module is almost always a
            // truncated copy (the publishing tools refuse both).
            var ebootSrc = Path.Combine(effectiveSource, "eboot.bin");
            if (File.Exists(ebootSrc) && new FileInfo(ebootSrc).Length < 64)
                throw new InvalidDataException($"eboot.bin is {new FileInfo(ebootSrc).Length} bytes — truncated or empty; nothing was built.");
            foreach (var m in Directory.EnumerateFiles(effectiveSource, "*", SearchOption.AllDirectories)
                         .Where(f => f.EndsWith(".prx", StringComparison.OrdinalIgnoreCase) || f.EndsWith(".sprx", StringComparison.OrdinalIgnoreCase))
                         .Where(f => { try { return new FileInfo(f).Length == 0; } catch { return false; } }))
                Console.Error.WriteLine($"[warn] {Path.GetRelativePath(effectiveSource, m).Replace('\\', '/')} is an empty file (truncated copy?); a game that loads it will crash");

            // sce_sys/keystone is the save-data key material: a package built with a
            // regenerated keystone (different passcode) reports every existing save of
            // the title as corrupt (verified, build N). LibProsperoPkg keeps a present
            // keystone and only generates one when it is missing — say which happened.
            if (File.Exists(Path.Combine(srcSceSys, "keystone")))
                Console.Error.WriteLine("  [keystone] source keystone preserved (save-data key; saves stay compatible with the original dump)");
            else
                Console.Error.WriteLine($"  [keystone] source has no sce_sys/keystone — the builder generates one for passcode {opts.Passcode}; saves from other builds of this title will NOT be readable");

            // Always stage (see the note above). Any failure in this block is fatal — a package
            // built from the raw source would silently lack the PlayGo validation, the license
            // entries, the fake-signing and the retail SELF flag. The one exception is the DDS
            // conversion, which runs LAST and is caught per icon.
            {
                string tempRoot = string.IsNullOrEmpty(opts.TemporaryDirectory) ? Path.GetTempPath() : opts.TemporaryDirectory!;
                if (stageInPlace)
                {
                    autoStage = effectiveSource;
                    Console.Error.WriteLine($"  [stage] staged in place: {autoStage} (the caller's working copy; nothing copied"
                                            + (consumeSource ? "; its files go as the image takes them in)" : ")"));
                }
                else
                {
                    // A real copy on every file system, never hard links (see MirrorSource).
                    autoStage = Path.Combine(tempRoot, "ffpfsc-stage-" + Guid.NewGuid().ToString("N").Substring(0, 8));
                    Directory.CreateDirectory(autoStage);
                    long stageBytes = DirectorySize(effectiveSource);
                    Console.Error.WriteLine($"[stage] copying {stageBytes / 1073741824.0:F1} GB into {autoStage} (the source stays untouched)");
                    MirrorSource(effectiveSource, autoStage, stageBytes);
                    Console.Error.WriteLine($"  [stage] mirrored source into {autoStage} (copy)");
                }

                // (0) Drop corrupt/mislabeled sce_sys metadata from the mirror (unlinks the
                //     hard link; the on-disk source is untouched). Shipping a corrupt PlayGo
                //     file means CE-100022-5 at launch, so a failed unlink is fatal.
                if (discardPlayGo || badJson.Length > 0)
                {
                    var stagedSceSys0 = Path.Combine(autoStage, "sce_sys");
                    foreach (var n in (discardPlayGo ? playgoPresent : Array.Empty<string>()).Concat(badJson))
                    {
                        var p = Path.Combine(stagedSceSys0, n);
                        if (File.Exists(p) || IsSymlink(p)) File.Delete(p);
                    }
                }

                // (b) Fake-sign raw ELFs. LibProsperoPkg's own MakeFself does the byte-
                // faithful conversion; skips SELFs and non-ELFs.
                if (elfsToSign.Length > 0)
                {
                    int signed = 0, skipped = 0;
                    foreach (var relPath in elfsToSign)
                    {
                        var srcFile = Path.Combine(effectiveSource, relPath);
                        var stagedFile = Path.Combine(autoStage, relPath);
                        try
                        {
                            var bytes = File.ReadAllBytes(srcFile);
                            if (!ProsperoFself.IsElf(bytes) || ProsperoFself.IsSelf(bytes)) { skipped++; continue; }
                            var fself = ProsperoFself.MakeFself(bytes, new FselfOptions());
                            // Replace the staged file (in place it is the source file itself, read above).
                            if (File.Exists(stagedFile) || IsSymlink(stagedFile)) File.Delete(stagedFile);
                            Directory.CreateDirectory(Path.GetDirectoryName(stagedFile)!);
                            File.WriteAllBytes(stagedFile, fself);
                            signed++;
                        }
                        catch (Exception ex)
                        {
                            skipped++;
                            Console.Error.WriteLine($"  [sign] skipped {relPath}: {ex.GetType().Name}: {ex.Message}");
                        }
                    }
                    Console.Error.WriteLine($"  [sign] fake-signed {signed} ELF(s); skipped {skipped} (already-SELF, non-ELF, or errors)");
                }

                // (c) Retail-normalize: two moves to make a "standard"-DRM retail dump
                // produce a package that a JB PS5 with kstuff-lite 1.13+ launches.
                //   (c.i) Drop placeholder license.dat/info from the stage so
                //         LibProsperoPkg's per-file iterator does not warn+skip on them.
                //   (c.ii) Install a LicenseProvider that returns a valid debug license
                //         for this contentId. LibProsperoPkg calls GetLicense() during
                //         CollectMediaEntries and yields the returned bytes as CNT
                //         entries 0x0400 (license.dat) and 0x0401 (license.info) — kills
                //         the "License missing" padlock on the PS5 homescreen.
                // drm_type=16 in the CNT header is handled UPSTREAM by our IL-patched
                // LibProsperoPkg.dll (patches/README.md: the Mono.Cecil patcher retargets the
                // branch of the hardcoded 0-for-Application ternary to always-16). This
                // lets us keep applicationDrmType="standard" verbatim in the embedded
                // param.json — flipping it to "upgradable" inside the pkg breaks games
                // whose source metadata declares an upgrade chain (originContentVersion,
                // targetContentVersion) because LibProsperoPkg strips those fields on
                // build → the console sees an "upgradable" app without a target and
                // refuses to launch with CE-100022-5.
                if (doRetailNormalize)
                {
                    var stagedSceSys = Path.Combine(autoStage, "sce_sys");
                    Directory.CreateDirectory(stagedSceSys);
                    foreach (var badFile in new[] { "license.dat", "license.info" })
                    {
                        var p = Path.Combine(stagedSceSys, badFile);
                        try { if (File.Exists(p) || IsSymlink(p)) File.Delete(p); } catch { }
                    }
                    opts.LicenseProvider = new DebugLicenseProvider(opts.ContentId);

                    // (c.iii-a) Patch SELF headers of eboot.bin and every prx to the retail
                    //           SELF pattern Sony's own toolchain stamps. Diffed the SELF
                    //           header (first 16 bytes) between a retail reference package's
                    //           eboot + libc.prx (launches on FW 11.60) and a source whose
                    //           executables were fake-signed by an older tool (fails with
                    //           CE-100022-5 post-icon):
                    //             retail:  ver=0x0110  ProgramType=0x10000101  info=0x05100530
                    //             source:  ver=0x0100  ProgramType=0x00000101  info=0x05100560
                    //           The ProgramType bit 0x10000000 is the "retail application"
                    //           flag the console loader gates launch on. Older fake-signers
                    //           stamp dev-flavor headers (0x00000101). We patch to retail
                    //           flavor in place — the fake signature is not verified on the
                    //           console anyway, and the patched bytes stay within the
                    //           SELF-header range the loader reads for its retail check.
                    //           sce_sys/about/right.sprx keeps the dev pattern: retail
                    //           packages have ProgramType=0x00000101 on that one file too —
                    //           it's a metadata SPRX that never executes.
                    // Surgical patch: only flip byte 0x0B (dev 0x00 → retail 0x10) to set
                    // ProgramType bit 0x10000000. Leaves HeaderSize/MetaSize + all other SELF
                    // header offsets untouched so the loader's parser reads exactly the
                    // structure the file actually has (patching version + info_field earlier
                    // caused HeaderSize/MetaSize to reparse to smaller values, which put the
                    // SDK-version record past the new-declared metadata boundary and confused
                    // the loader).
                    int selfPatched = 0, selfLeft = 0;
                    foreach (var relPath in Directory.EnumerateFiles(autoStage, "*", SearchOption.AllDirectories))
                    {
                        var name = Path.GetFileName(relPath);
                        var ext = Path.GetExtension(relPath).ToLowerInvariant();
                        var relFromRoot = Path.GetRelativePath(autoStage, relPath).Replace('\\','/');
                        bool isLoadable = name == "eboot.bin" || ext == ".prx" || ext == ".sprx";
                        if (!isLoadable) continue;
                        if (relFromRoot.StartsWith("sce_sys/about/", StringComparison.OrdinalIgnoreCase)) continue;
                        try
                        {
                            byte[] head = new byte[16];
                            using (var fs = new FileStream(relPath, FileMode.Open, FileAccess.Read))
                            {
                                int n = fs.Read(head, 0, 16);
                                if (n < 16 || head[0] != 0x54 || head[1] != 0x14 || head[2] != 0xF5 || head[3] != 0xEE)
                                { selfLeft++; continue; }
                            }
                            if (head[0x0B] == 0x10) { selfLeft++; continue; }
                            var backup = relPath + ".pre-retail";
                            File.Copy(relPath, backup, overwrite: true);
                            File.Delete(relPath);
                            File.Move(backup, relPath);
                            using (var fs = new FileStream(relPath, FileMode.Open, FileAccess.Write))
                            {
                                fs.Seek(0x0B, SeekOrigin.Begin);
                                fs.WriteByte(0x10);
                            }
                            selfPatched++;
                        }
                        catch (Exception ex) { Console.Error.WriteLine($"  [self-hdr] skipped {relFromRoot}: {ex.GetType().Name}: {ex.Message}"); selfLeft++; }
                    }
                    Console.Error.WriteLine($"  [self-hdr] set retail bit (byte 0x0B: 0x10) on {selfPatched} eboot.bin/*.prx; left {selfLeft} untouched");

                    // (c.iii) Rewrite staged param.json: attribute bit 29 per --hdr-flag and a
                    //         "kernel" block when the source has none (see RewriteStagedParamJson).
                    RewriteStagedParamJson(srcSceSys, stagedSceSys, hdrFlag, addKernel: true, tag: "retail");
                    Console.Error.WriteLine("  [retail] dropped placeholder license.dat/info; installed DebugLicenseProvider (CNT entries 0x0400/0x0401); drm_type=16 via patched LibProsperoPkg.dll");
                }

                // (d) --hdr-flag on|off for every other source (free DRM, or --no-retail-
                //     normalize): the attribute rewrite used to live only inside the retail
                //     block, so the flag was silently ignored there. auto changes nothing.
                if (!doRetailNormalize && hdrFlag != "auto")
                    RewriteStagedParamJson(srcSceSys, Path.Combine(autoStage!, "sce_sys"), hdrFlag, addKernel: false, tag: "param");

                // (d2) Presentation PNGs: icon0/pic0/pic1 8-bit RGB, pic2 8-bit RGBA. A PNG that
                //      is not a PNG or is in the wrong mode is regenerated from its .dds (or
                //      converted / left out when there is none) — see PresentationMedia.cs.
                PresentationMedia.Normalize(Path.Combine(autoStage, "sce_sys"), m => Console.Error.WriteLine(m));

                // (e) DDS icon CNT entries — caught per icon: a PNG that Magick rejects must not
                //     abort the safety-critical steps above. The package then lacks that DDS
                //     (without icon0.dds it installs but never launches). Checked after (d2) on
                //     the staged files: a DDS that is missing, not DX10/BC7, or not the PNG's
                //     size (a stale or mislabeled one from a dump) is generated from the PNG.
                var ddsWork = new List<(string Name, string Why)>();
                {
                    var stagedSceSysE = Path.Combine(autoStage, "sce_sys");
                    if (Directory.Exists(stagedSceSysE))
                        foreach (var f in Directory.EnumerateFiles(stagedSceSysE, "*.png").OrderBy(x => x, StringComparer.Ordinal))
                        {
                            var n = Path.GetFileName(f);
                            var ddsName = Path.ChangeExtension(n, ".dds");
                            if (PresentationMedia.Kind(n) == null || !EntryNames.NameToId.ContainsKey(ddsName)) continue;
                            var ddsPath = Path.Combine(stagedSceSysE, ddsName);
                            var why = File.Exists(ddsPath)
                                ? PresentationMedia.DdsProblem(ddsPath, PresentationMedia.ReadPng(f))
                                : "missing";
                            if (why != null) ddsWork.Add((n, why));
                        }
                }
                if (ddsWork.Count > 0)
                {
                    var stagedSceSys = Path.Combine(autoStage, "sce_sys");
                    foreach (var (name, why) in ddsWork)
                    {
                        try
                        {
                            var stagedPng = Path.Combine(stagedSceSys, name);   // after (d2)
                            var png = File.ReadAllBytes(stagedPng);
                            var dds = ProsperoDdsEncoder.EncodePngToDds(png, opts.TemporaryDirectory ?? Path.GetTempPath());
                            var ddsPath = Path.Combine(stagedSceSys, Path.ChangeExtension(name, ".dds"));
                            if (File.Exists(ddsPath) || IsSymlink(ddsPath)) File.Delete(ddsPath);
                            File.WriteAllBytes(ddsPath, dds);
                            Console.Error.WriteLine($"  [icon] generated sce_sys/{Path.ChangeExtension(name, ".dds")} ({dds.Length:N0} B) from {name}"
                                + (why == "missing" ? "" : $" (the shipped one: {why})"));
                        }
                        catch (Exception ex)
                        {
                            Console.Error.WriteLine($"[warn] icon conversion failed for {name}: {ex.GetType().Name}: {ex.Message}"
                                + (name == "icon0.png" ? " — the package will have no icon0.dds and will not launch on a console" : ""));
                        }
                    }
                }

                // (f) ampr_emu.index — the AMPR emulator (fakelib/libSceAmpr.sprx) resolves APR
                //     file ids through /app0/ampr_emu.index, so the index must describe the files
                //     that are actually packed: fake-signing above changes sizes, and a source may
                //     ship no index at all. Rebuilt over the staged tree, written by rename so a
                //     hard-linked index from the source is replaced, never written through.
                if (amprIndex && File.Exists(Path.Combine(autoStage, "fakelib", "libSceAmpr.sprx")))
                {
                    // Every packaged file carries the build timestamp as its inode time (the
                    // library's TimeStamp, the Unix epoch unless set): record exactly that.
                    long pkgTime = (long)opts.TimeStamp.ToUniversalTime().Subtract(DateTime.UnixEpoch).TotalSeconds;
                    // The builder rewrites param.json while packing (publisher fields, SDK and
                    // firmware version text); the console serves that version. Apply the same
                    // rewrite to the staged copy now so the index records its real size. The
                    // rewrite is idempotent, so the builder's own pass leaves it unchanged.
                    PublishParamJson(autoStage, opts);
                    int rows = AmprIndex.Write(autoStage, pkgTime, packageView: true);
                    Console.Error.WriteLine(rows > 0
                        ? $"  [ampr] rebuilt ampr_emu.index over the packed files ({rows:N0} file(s))"
                        : "  [ampr] nothing to index; ampr_emu.index left as it is");
                }

                // (g) PlayGo set vs. the packed tree. The builder keeps a complete prepared set
                //     verbatim, so a set made for another file tree (a backport adds files after
                //     the dump; staging adds or drops some) would describe the wrong files. The
                //     hash table lists the path hash of every file in the inner image and
                //     playgo-ficm.dat one 2-byte chunk id per file: compare both with the tree
                //     the builder will pack and let it regenerate the set on any mismatch.
                //     scenario.json is dropped alongside the core set when it cannot be preserved.
                {
                    var sg = Path.Combine(autoStage, "sce_sys");
                    var pg = new[] { "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat" }
                        .Select(n => Path.Combine(sg, n)).ToArray();
                    var scenarioPath = Path.Combine(sg, "playgo-scenario.json");
                    if (pg.All(File.Exists))
                    {
                        var paths = InnerImagePaths(autoStage);
                        var why = PlayGoMismatch(paths, File.ReadAllBytes(pg[0]), File.ReadAllBytes(pg[1]), File.ReadAllBytes(pg[2]));
                        if (why == null)
                        {
                            int keptChunks = detectedPlayGoChunks > 0 ? detectedPlayGoChunks : opts.PlayGoChunkCount;
                            Console.Error.WriteLine($"  [playgo] prepared set matches the packed files ({paths.Count:N0} file(s), {keptChunks} chunk(s)); kept — the library keeps its file assignments, labels and languages and rebuilds the image ranges"
                                + (File.Exists(scenarioPath) ? " (with scenario.json)" : ""));
                        }
                        else
                        {
                            foreach (var f in pg) File.Delete(f);   // staged links only; the source is untouched
                            if (File.Exists(scenarioPath)) File.Delete(scenarioPath);
                            Console.Error.WriteLine($"  [playgo] prepared set does not match the packed files ({why}); discarded so LibProsperoPkg regenerates a consistent {opts.PlayGoChunkCount}-chunk set");
                        }
                    }
                }

                // (h) License for a title the builder describes itself: without a param.json it
                //     generates one with applicationDrmType "standard" (the same happens when
                //     the field is absent), and a standard title without license entries shows
                //     a padlock and does not start. Issue the debug license for it too.
                if (opts.LicenseProvider == null)
                {
                    var pjStaged = Path.Combine(autoStage, "sce_sys", "param.json");
                    string? drmStaged = null;
                    if (File.Exists(pjStaged))
                        try
                        {
                            using var d = JsonDocument.Parse(File.ReadAllBytes(pjStaged));
                            drmStaged = d.RootElement.TryGetProperty("applicationDrmType", out var dt) ? dt.GetString() : null;
                        }
                        catch (JsonException) { drmStaged = "?"; }
                    if (drmStaged == null)
                    {
                        opts.LicenseProvider = new DebugLicenseProvider(opts.ContentId);
                        Console.Error.WriteLine("  [license] " + (File.Exists(pjStaged) ? "param.json has no applicationDrmType" : "no sce_sys/param.json")
                            + " — the builder makes it \"standard\"; issuing a debug license (CNT entries 0x0400/0x0401)");
                    }
                }

                opts.SourceFolder = autoStage;
            }
        }
        catch (Exception ex)
        {
            var chain = ex.GetType().Name + ": " + ex.Message;
            for (var e = ex.InnerException; e != null; e = e.InnerException)
                chain += "  <- " + e.GetType().Name + ": " + e.Message;
            Console.Error.WriteLine("[error] source staging failed (" + chain + "); nothing was built. A package built from the raw source would lack PlayGo validation, license entries, fake-signing and the retail SELF flag.");
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine(ex.ToString());
            if (!stageInPlace) CleanupStage(autoStage);
            return 1;
        }

        Console.Error.WriteLine($"[info] build  {opts.SourceFolder} -> {opts.OutputFolder}  (inner={opts.InnerCompression}, backend={opts.KrakenBackend}, level={opts.KrakenCompressionLevel}, workers={(opts.KrakenMaxDegreeOfParallelism == 0 ? $"auto ({Environment.ProcessorCount})" : opts.KrakenMaxDegreeOfParallelism.ToString())}, temp={opts.TemporaryDirectory ?? "$TMPDIR"}, chunks={opts.PlayGoChunkCount}, fake-sign={autoFakeSign}, retail-normalize={doRetailNormalize})");

        // Baselines for the cleanup after a failed or cancelled build: only files that did not
        // exist before the build are removed — a .pkg in the output folder (the library writes
        // the FIH image straight under its final name) and the library's intermediates in temp.
        string outFull = Path.GetFullPath(opts.OutputFolder);
        string tempFull = Path.GetFullPath(string.IsNullOrWhiteSpace(opts.TemporaryDirectory) ? Path.GetTempPath() : opts.TemporaryDirectory!);
        var pkgBefore = new HashSet<string>(SafeEnumerate(outFull, "*.pkg"), StringComparer.Ordinal);
        var tempBefore = new HashSet<string>(LibraryTempFiles(tempFull), StringComparer.Ordinal);
        void CleanupAfterFailure()
        {
            if (!stageInPlace) CleanupStage(autoStage);
            foreach (var p in SafeEnumerate(outFull, "*.pkg")) if (!pkgBefore.Contains(p)) TryDeleteFile(p, "partial package");
            foreach (var p in LibraryTempFiles(tempFull)) if (!tempBefore.Contains(p)) TryDeleteFile(p, "library temp file");
        }

        // SIGTERM (what the GUI's cancel sends to the process group) and SIGINT cancel the build
        // through the library's own CancellationToken, then everything this run created is
        // removed. The library polls the token per file and per block, so it normally unwinds
        // within a second; the GUI follows up with SIGKILL after 5 s, so if the build has not
        // unwound in 2.5 s the handler thread cleans up itself and exits.
        using var cts = new CancellationTokenSource();
        opts.CancellationToken = cts.Token;
        int signalExit = 0;
        using var buildDone = new ManualResetEventSlim(false);
        void OnSignal(PosixSignalContext ctx)
        {
            ctx.Cancel = true;   // the runtime must not terminate the process for us
            int code = ctx.Signal == PosixSignal.SIGINT ? 130 : 143;
            if (Interlocked.CompareExchange(ref signalExit, code, 0) != 0) return;
            try { Console.Error.WriteLine($"[cancel] {ctx.Signal} received: cancelling the build and cleaning up"); } catch { }
            cts.Cancel();
            if (!buildDone.Wait(TimeSpan.FromMilliseconds(2500)))
            {
                CleanupAfterFailure();
                Environment.Exit(code);
            }
        }
        using var onTerm = PosixSignalRegistration.Create(PosixSignal.SIGTERM, OnSignal);
        using var onInt = PosixSignalRegistration.Create(PosixSignal.SIGINT, OnSignal);

        ProsperoBuildResult result;
        try
        {
            long consumedBytes = 0; int consumedFiles = 0;
            var packedLine = new System.Text.RegularExpressions.Regex(@"\[inner\]\s+data\s+\d+% \(\d+/\d+\): (/\S+) ->");
            void Log(string s)
            {
                Console.Error.WriteLine("  " + s);
                if (!(consumeSource && stageInPlace && autoStage != null)) return;
                var m = packedLine.Match(s);
                if (!m.Success) return;
                // The library names a file once it sits in the inner image and never reads it
                // again (verified: a build that deleted each one this way gave the identical
                // deterministic package). Only files inside the stage, never anything else.
                var rel = m.Groups[1].Value.TrimStart('/').Replace('/', Path.DirectorySeparatorChar);
                var f = Path.GetFullPath(Path.Combine(autoStage, rel));
                if (!IsInside(f, autoStage) || !File.Exists(f)) return;
                try
                {
                    long len = new FileInfo(f).Length;
                    File.Delete(f);
                    consumedBytes += len; consumedFiles++;
                    Console.Error.WriteLine($"  [consume] freed {consumedBytes / 1073741824.0:F1} GB so far ({consumedFiles} file(s) the image already holds)");
                }
                catch (Exception ex) { Console.Error.WriteLine($"  [consume] could not remove {rel}: {ex.Message}"); }
            }
            result = ProsperoPackageBuilder.Build(opts, logger: Log);
        }
        catch (Exception) when (signalExit != 0)
        {
            buildDone.Set();
            CleanupAfterFailure();
            try { Console.Error.WriteLine($"[cancel] build cancelled; mirror, temp files and partial output removed (exit {signalExit})"); } catch { }
            return signalExit;
        }
        catch
        {
            buildDone.Set();
            CleanupAfterFailure();
            throw;
        }
        finally
        {
            buildDone.Set();
            if (!stageInPlace) CleanupStage(autoStage);
        }
        Console.WriteLine($"OK — wrote {result.OutputPath} ({new FileInfo(result.OutputPath).Length:N0} B)");
        if (result.Warnings != null)
            foreach (var w in result.Warnings) Console.Error.WriteLine("[warn] " + w);
        return 0;
    }

    /// <summary>Rewrite the staged sce_sys/param.json from the source's copy: attribute bit 29
    /// (0x20000000, HDR support) per --hdr-flag — auto keeps the source's declaration, the
    /// publisher's intent, which a console on "HDR when supported" follows; on/off set/clear
    /// it — and, with <paramref name="addKernel"/> (retail-normalize), a "kernel" block (a
    /// retail reference package's cpu/gpu page-table + flexible-memory sizes) when the source
    /// has none; not launch-critical, kept to match retail packages, a source's own block wins.
    /// The staged file is only replaced when something changes, so the hard link to the source
    /// stays in place otherwise.</summary>
    static void RewriteStagedParamJson(string srcSceSys, string stagedSceSys, string hdrFlag, bool addKernel, string tag)
    {
        var srcParam = Path.Combine(srcSceSys, "param.json");
        var stagedParam = Path.Combine(stagedSceSys, "param.json");
        if (!File.Exists(srcParam))
        {
            if (hdrFlag != "auto")
                Console.Error.WriteLine($"[warn] --hdr-flag {hdrFlag} ignored: the source has no sce_sys/param.json (the builder generates a minimal one)");
            return;
        }
        using var pdoc = JsonDocument.Parse(File.ReadAllBytes(srcParam));
        long currentAttr = 0;
        bool hasAttribute = pdoc.RootElement.TryGetProperty("attribute", out var at);
        if (hasAttribute) currentAttr = at.GetInt64();
        const long HdrBit = 0x20000000L;
        bool srcHdr = (currentAttr & HdrBit) != 0;
        long newAttr = hdrFlag switch
        {
            "on"  => currentAttr | HdrBit,
            "off" => currentAttr & ~HdrBit,
            _     => currentAttr,
        };
        string hdrNote = hdrFlag switch
        {
            "on"  => srcHdr ? "HDR support flag already set in source (--hdr-flag on)" : "HDR support flag set (--hdr-flag on; the source did not declare it)",
            "off" => srcHdr ? "HDR support flag CLEARED (--hdr-flag off; the source declared it)" : "HDR support flag absent in source, left absent (--hdr-flag off)",
            _     => srcHdr ? "source declares HDR support (attribute bit 29) — kept" : "source does not declare HDR support (attribute bit 29) — kept as is; pass --hdr-flag on to force HDR output",
        };
        Console.Error.WriteLine($"  [{tag}] {hdrNote}");
        bool hasKernel = pdoc.RootElement.TryGetProperty("kernel", out _);
        bool wantKernel = addKernel && !hasKernel;
        bool addAttr = !hasAttribute && newAttr != currentAttr;   // the source has no "attribute" at all
        if (newAttr == currentAttr && !wantKernel) return;

        var buf = new MemoryStream();
        using (var w = new Utf8JsonWriter(buf, new JsonWriterOptions { Indented = true }))
        {
            void WriteKernel()
            {
                w.WriteStartObject("kernel");
                w.WriteNumber("cpuPageTableSize", 67108864);       // 64 MiB
                w.WriteNumber("flexibleMemorySize", 272629760);    // ~260 MiB (retail reference value)
                w.WriteNumber("gpuPageTableSize", 67108864);       // 64 MiB
                w.WriteEndObject();
            }
            w.WriteStartObject();
            bool kernelWritten = false, attrWritten = false;
            foreach (var prop in pdoc.RootElement.EnumerateObject())
            {
                // Keep source order; the kernel block goes just before "localizedParameters"
                // (typical Sony ordering) when it is missing.
                if (wantKernel && !kernelWritten && prop.Name == "localizedParameters") { WriteKernel(); kernelWritten = true; }
                if (prop.Name == "attribute") { w.WriteNumber("attribute", newAttr); attrWritten = true; }
                else prop.WriteTo(w);
                // A source without "attribute": add it after applicationDrmType (Sony's ordering).
                if (addAttr && !attrWritten && prop.Name == "applicationDrmType") { w.WriteNumber("attribute", newAttr); attrWritten = true; }
            }
            if (addAttr && !attrWritten) w.WriteNumber("attribute", newAttr);
            if (wantKernel && !kernelWritten) WriteKernel();
            w.WriteEndObject();
        }
        if (File.Exists(stagedParam) || IsSymlink(stagedParam)) File.Delete(stagedParam);
        Directory.CreateDirectory(stagedSceSys);
        File.WriteAllBytes(stagedParam, buf.ToArray());
        var kernelNote = wantKernel ? "; added kernel{cpuPageTable=64Mi, flex=260Mi, gpuPageTable=64Mi}" : "";
        var attrNote = newAttr != currentAttr ? $"param.json attribute 0x{currentAttr:X8} -> 0x{newAttr:X8}" : $"param.json attribute 0x{currentAttr:X8} unchanged";
        Console.Error.WriteLine($"  [{tag}] {attrNote}{kernelNote}");
    }

    static void CleanupStage(string? stage)
    {
        if (stage == null) return;
        try { if (Directory.Exists(stage)) Directory.Delete(stage, recursive: true); } catch { }
    }

    static List<string> SafeEnumerate(string dir, string pattern)
    {
        try { return Directory.Exists(dir) ? Directory.EnumerateFiles(dir, pattern).ToList() : new List<string>(); }
        catch { return new List<string>(); }
    }

    /// <summary>LibProsperoPkg's intermediates in the temp directory: libprospero-publisher-&lt;guid&gt;.*
    /// (pfs_image.dat, naps_pkg_layout.dat, outer.pfs) and .&lt;pkg name&gt;.&lt;guid&gt;.cnt.tmp.</summary>
    static IEnumerable<string> LibraryTempFiles(string tempDir)
        => SafeEnumerate(tempDir, "libprospero-publisher-*").Concat(SafeEnumerate(tempDir, ".*.cnt.tmp"));

    static void TryDeleteFile(string path, string what)
    {
        try
        {
            if (!File.Exists(path)) return;
            File.Delete(path);
            Console.Error.WriteLine($"  [cleanup] removed {what}: {path}");
        }
        catch { }
    }

    /// <summary>Replace the staged sce_sys/param.json with the version the builder stores in the
    /// package (LibProsperoPkg's internal publisher rewrite: padded versionFileUri, pubtools block,
    /// SDK and firmware version text). Best effort: if the library changes and the rewrite cannot be
    /// reached, the staged file stays as it is and a warning says so.</summary>
    static void PublishParamJson(string stage, ProsperoBuildOptions opts)
    {
        var path = Path.Combine(stage, "sce_sys", "param.json");
        if (!File.Exists(path)) return;
        try
        {
            var rewrite = typeof(ProsperoPkgBuilder).GetMethod("BuildPublisherParamJson", BindingFlags.NonPublic | BindingFlags.Static)
                ?? throw new MissingMethodException("ProsperoPkgBuilder.BuildPublisherParamJson");
            var props = new ProsperoPkgBuildProperties
            {
                SourceFolder = stage,
                ContentId = opts.ContentId,
                VolumeType = ProsperoPackageBuilder.ProsperoVolumeTypeForMode(opts.Mode),
                TimeStamp = opts.TimeStamp,
                SdkVersionOverride = opts.SdkVersionOverride,
            };
            var bytes = (byte[])rewrite.Invoke(null, new object[] { stage, props })!;
            if (bytes.AsSpan().SequenceEqual(File.ReadAllBytes(path))) return;
            File.Delete(path);                      // never write through a hard link into the source
            File.WriteAllBytes(path, bytes);
        }
        catch (Exception ex)
        {
            var inner = ex is TargetInvocationException { InnerException: { } ie } ? ie : ex;
            Console.Error.WriteLine($"[warn] could not pre-apply the package's param.json rewrite ({inner.GetType().Name}: {inner.Message}); "
                + "ampr_emu.index may record the source size of sce_sys/param.json");
        }
    }

    /// <summary>The files of the inner image as LibProsperoPkg lays it out ("/" + path, no .gp4/
    /// .gp5, no top-level "decrypted", no sce_sys/ext_info.dat, no sce_sys entry that goes into
    /// the metadata table, plus the sce_sys/about/right.sprx the builder always adds).</summary>
    static List<string> InnerImagePaths(string stage)
    {
        var list = new List<string>();
        foreach (var f in Directory.EnumerateFiles(stage, "*", SearchOption.AllDirectories))
        {
            var rel = "/" + Path.GetRelativePath(stage, f).Replace('\\', '/');
            var name = Path.GetFileName(rel);
            if (FsJunk.PathHasJunk(rel)) continue;
            if (name.EndsWith(".gp4", StringComparison.OrdinalIgnoreCase) || name.EndsWith(".gp5", StringComparison.OrdinalIgnoreCase)) continue;
            if (rel.StartsWith("/decrypted/", StringComparison.OrdinalIgnoreCase)) continue;
            if (rel.Equals("/sce_sys/ext_info.dat", StringComparison.OrdinalIgnoreCase)) continue;
            if (rel.StartsWith("/sce_sys/", StringComparison.Ordinal))
            {
                var t = rel["/sce_sys/".Length..];
                if (t == "param.json" || EntryNames.NameToId.ContainsKey(t)) continue;
            }
            list.Add(rel);
        }
        if (!list.Contains("/sce_sys/about/right.sprx")) list.Add("/sce_sys/about/right.sprx");
        return list;
    }

    /// <summary>Why a prepared PlayGo hash table + ficm do not describe <paramref name="paths"/>
    /// (null = they do).</summary>
    // Mirrors what ProsperoPlayGo.ReadSourceLayout requires, so a set that would make the
    // library throw is discarded here and regenerated instead.
    static string? PlayGoMismatch(IReadOnlyList<string> paths, byte[] chunkDat, byte[] hashTable, byte[] ficm)
    {
        if (!LooksLikePlayGoChunkDat(chunkDat) || !LooksLikePlayGoHashTable(hashTable) || !LooksLikePlayGoFicm(ficm)) return "bad format";
        uint count = U32(hashTable, 36);
        if (56L + count * 8L != hashTable.Length) return "hash-table count does not match its size";
        var have = new HashSet<ulong>();
        ulong prev = 0;
        for (int i = 0; i < count; i++)
        {
            ulong hsh = System.Buffers.Binary.BinaryPrimitives.ReadUInt64LittleEndian(hashTable.AsSpan(56 + i * 8, 8));
            if (i > 0 && hsh <= prev) return "hash table is not sorted strictly ascending";
            have.Add(hsh); prev = hsh;
        }
        var want = new HashSet<ulong>(paths.Select(ProsperoPs5FlatPathTable.HashPath));
        int notListed = want.Count(x => !have.Contains(x)), notPacked = have.Count(x => !want.Contains(x));
        if (notListed > 0 || notPacked > 0)
            return $"{notListed} packed file(s) missing from its hash table, {notPacked} listed file(s) not packed";
        uint ficmSlots = U32(ficm, 12);
        if (ficmSlots != 2u * (uint)paths.Count) return $"ficm covers {ficmSlots / 2} file(s), the image has {paths.Count}";
        int chunkCount = chunkDat[0x0A] | (chunkDat[0x0B] << 8);
        for (int i = 0; i + 1 < ficmSlots; i += 2)
        {
            int id = ficm[16 + i] | (ficm[16 + i + 1] << 8);
            if (id >= chunkCount) return $"ficm assigns a file to chunk {id} but the set declares {chunkCount} chunk(s)";
        }
        return null;
    }

    /// <summary>LicenseProvider that issues a valid debug license.dat/info pair for a given
    /// content-id. Wraps LibProsperoPkg.PKG.ProsperoSystemFiles.BuildDebugLicense — the exact
    /// generator LibProsperoPkg itself uses at PackageBuilder.cs when a "standard" Application
    /// source has no license files. We hand it back for the "upgradable" trick path where
    /// the source-file iterator would otherwise emit nothing, so the console gets
    /// well-formed CNT entries 0x0400 (license.dat) + 0x0401 (license.info).</summary>
    sealed class DebugLicenseProvider : IProsperoLicenseProvider
    {
        private readonly string _contentId;
        public DebugLicenseProvider(string contentId) { _contentId = contentId; }
        public ProsperoLicenseArtifacts GetLicense(ProsperoLicenseRequest request)
        {
            var key = request.EntitlementKey ?? Array.Empty<byte>();
            return ProsperoSystemFiles.BuildDebugLicense(request.VolumeType, _contentId, key);
        }
    }

    // PlayGo on-wire formats, mirrored from LibProsperoPkg.ProsperoPlayGo (which is what
    // Sony's publisher emits too — verified against an untouched retail package).
    static uint U32(byte[] b, int o) => (uint)(b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24));

    /// <summary>playgo-chunk.dat: "plgx" magic, u32 total size at 0x10 equals the length.</summary>
    static bool LooksLikePlayGoChunkDat(byte[] b)
        => b.Length >= 0x40 && b[0] == (byte)'p' && b[1] == (byte)'l' && b[2] == (byte)'g' && b[3] == (byte)'x'
           && U32(b, 0x10) == (uint)b.Length;

    /// <summary>playgo-hash-table.dat: u32 1, u32 0x08000000, u32 56 (header), u32 table
    /// size, "\x7fFLT" at 0x18; 56 + table size equals the length.</summary>
    static bool LooksLikePlayGoHashTable(byte[] b)
        => b.Length >= 56 && U32(b, 0) == 1u && U32(b, 4) == 0x08000000u && U32(b, 8) == 56u
           && b[24] == 0x7F && b[25] == (byte)'F' && b[26] == (byte)'L' && b[27] == (byte)'T'
           && 56u + U32(b, 12) == (uint)b.Length;

    /// <summary>playgo-ficm.dat: u32 1, u32 16 (header) at 0x08, u32 payload size at 0x0C;
    /// 16 + payload equals the length.</summary>
    static bool LooksLikePlayGoFicm(byte[] b)
        => b.Length >= 16 && U32(b, 0) == 1u && U32(b, 8) == 16u && 16u + U32(b, 12) == (uint)b.Length;

    // LibProsperoPkg refuses an extraction target with a symlink anywhere in its path
    // (macOS: /var -> /private/var, /tmp -> /private/tmp). Hand it the resolved path;
    // components that do not exist yet are appended unresolved.
    static string RealPath(string path)
    {
        var full = Path.GetFullPath(path);
        if (OperatingSystem.IsWindows()) return full;
        var missing = new Stack<string>();
        var probe = full;
        while (!Directory.Exists(probe))
        {
            var parent = Path.GetDirectoryName(probe);
            if (string.IsNullOrEmpty(parent) || parent == probe) return full;
            missing.Push(Path.GetFileName(probe));
            probe = parent;
        }
        var ptr = realpath(probe, IntPtr.Zero);
        if (ptr == IntPtr.Zero) return full;
        try { probe = Marshal.PtrToStringUTF8(ptr) ?? probe; } finally { free(ptr); }
        while (missing.Count > 0) probe = Path.Combine(probe, missing.Pop());
        return probe;
    }
    [DllImport("libc")] static extern IntPtr realpath(string path, IntPtr resolved);
    [DllImport("libc")] static extern void free(IntPtr p);

    static bool LooksLikePlayGoScenarioJson(byte[] b)
    {
        try
        {
            using var d = JsonDocument.Parse(b);
            var r = d.RootElement;
            if (r.ValueKind != JsonValueKind.Object) return false;
            if (!r.TryGetProperty("scenarioCount", out var sc) || sc.ValueKind != JsonValueKind.Number) return false;
            if (!r.TryGetProperty("scenarioDefaultId", out _)) return false;
            if (!r.TryGetProperty("scenarioDefaultLanguage", out _)) return false;
            int count = sc.GetInt32();
            return count >= 1 && count <= 5;
        }
        catch { return false; }
    }

    static bool LooksLikeJsonText(byte[] b)
    {
        try { using var d = JsonDocument.Parse(b); return d.RootElement.ValueKind == JsonValueKind.Object || d.RootElement.ValueKind == JsonValueKind.Array; }
        catch { return false; }
    }

    /// <summary>Return relative paths (from *root*) of every regular file whose first bytes
    /// look like a raw ELF (magic 0x7F 'E' 'L' 'F'). Only files with plausible extensions
    /// (eboot.bin, .elf, .prx, .sprx) are checked so a huge blob is not sniffed unnecessarily.</summary>
    static string[] FindRawElfs(string root)
    {
        var result = new List<string>();
        var head = new byte[4];
        foreach (var f in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories))
        {
            var name = Path.GetFileName(f);
            var ext = Path.GetExtension(f).ToLowerInvariant();
            if (FsJunk.PathHasJunk(Path.GetRelativePath(root, f))) continue;
            if (name != "eboot.bin" && ext != ".elf" && ext != ".prx" && ext != ".sprx") continue;
            try
            {
                using var fs = File.OpenRead(f);
                if (fs.Read(head, 0, 4) != 4) continue;
                if (head[0] == 0x7F && head[1] == (byte)'E' && head[2] == (byte)'L' && head[3] == (byte)'F')
                    result.Add(Path.GetRelativePath(root, f));
            }
            catch { }
        }
        return result.ToArray();
    }
}

[System.Text.Json.Serialization.JsonSourceGenerationOptions(WriteIndented = false)]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.ListInnerDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.MembersResultDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.InspectDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.ExtractResultDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.ValidateReportDoc))]
internal sealed partial class PkgToolJsonContext : System.Text.Json.Serialization.JsonSerializerContext
{
    static PkgToolJsonContext? _indented;
    /// <summary>Same contract, pretty-printed — for the human-facing --json outputs.</summary>
    public static PkgToolJsonContext Indented => _indented ??= new PkgToolJsonContext(new JsonSerializerOptions { WriteIndented = true });
}
