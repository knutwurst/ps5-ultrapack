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

// ffpfsc-pkg-tool: thin CLI over drakmor's LibProsperoPkg for fPKG extract, inspect, build

internal static partial class Program
{
    const string ToolVersion = "2.3.1";

    static int Main(string[] args)
    {
        // UTF-8 everywhere: Windows consoles and pipes still default to the OEM code page. in 2026
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        SafeLineWriter.Install();                      // after the encoding: setting it replaces the writers
        // invariant number format everywhere, the build log gets parsed
        var invariant = System.Globalization.CultureInfo.InvariantCulture;
        System.Globalization.CultureInfo.DefaultThreadCurrentCulture = invariant;
        System.Globalization.CultureInfo.DefaultThreadCurrentUICulture = invariant;
        System.Globalization.CultureInfo.CurrentCulture = invariant;
        System.Globalization.CultureInfo.CurrentUICulture = invariant;
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
                "ps4-keystone" => CmdPs4Keystone(args),
                "ps4-rebind" => CmdPs4Rebind(args),
                "ps4-sfo" => CmdPs4Sfo(args),
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
        Console.WriteLine("  ps4-keystone <ps4.pkg> [<keystone>] [--undo FILE]  (no keystone: print the current one)");
        Console.WriteLine("      Writes a 96-byte keystone (e.g. exported from a save) into a PS4 fake package, in place.");
        Console.WriteLine("      --undo FILE keeps the overwritten bytes; 'ps4-keystone <ps4.pkg> --restore FILE' puts them back.");
        Console.WriteLine("  ps4-sfo <ps4.pkg> --set KEY=VALUE [--set ...] [--undo FILE]  (change param.sfo values in place)");
        Console.WriteLine("  ps4-rebind <update.pkg> <base.pkg> [--undo FILE]  (point an update at a changed base again)");
        Console.WriteLine("      Copies the base's content and game digest into the update; restore with ps4-keystone --restore.");
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
        Console.WriteLine("      --keystone FILE              put FILE (96 bytes) into the package as sce_sys/keystone instead of the source's");
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
            // sce_sys metadata (icons, sound, trophies, PlayGo) lives in the CNT, not the inner image
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

        // param.json, icon0, playgo-*.dat live in the CNT, not the PFS; no merge, no game: fail
        int mergedCount = 0;
        if (inner && mergeCnt)
        {
            var tmpCnt = Path.Combine(outDir, ".cnt-tmp-" + Guid.NewGuid().ToString("N"));
            var errors = new List<string>();
            try
            {
                foreach (var kv in LiftCntSceSys(pkg, passcode, tmpCnt, errors))
                {
                    var dst = SafeTarget(outDir, kv.Key);
                    Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                    File.Copy(kv.Value, dst, overwrite: true);
                    mergedCount++;
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidDataException)
            {
                errors.Add("CNT metadata merge failed: " + ex.Message);
            }
            finally
            {
                try { Directory.Delete(tmpCnt, recursive: true); } catch { }
            }
            if (errors.Count > 0)
            {
                foreach (var e in errors) Console.Error.WriteLine("[error] " + e);
                Console.Error.WriteLine($"[error] extract failed: the sce_sys metadata (param.json, icons, PlayGo) could not be added, so {outDir} is not a complete game folder");
                return 1;
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

    // files of /app0/sce_sys only, never the builder's own entries (.digests, bare ids)
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

    // full extract, list-inner and selective extract all use this, so they agree on the tree
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

    // source-generated JSON, so it works whether the binary is trimmed or not
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

    // rejects what the library rejects: empty, '.', '..' segments
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

    // CNT-lifted sce_sys files win over same-named PFS files, like the full extract
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
            // 4 MiB cache blocks: metadata needs few, file data takes the chunked fast path anyway
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
                    Console.WriteLine($"[####] {pct}% extract ({SafeLineWriter.Escape(rel)})");
                }
            }
            foreach (var e in fileTargets)
            {
                var dst = SafeTarget(outDir, e.path);
                string got;
                if (e.source == "cnt")
                {
                    got = WriteExtractedFile(dst, o => { using var i = File.OpenRead(cnt[e.path]); i.CopyTo(o); }, keepExisting: true);
                    cntCount++;
                    Progress(e.size ?? 0, e.path);
                }
                else
                {
                    var node = img.Pfs.GetFile(e.path) ?? throw new InvalidDataException("inner file vanished: " + e.path);
                    got = WriteExtractedFile(dst, o => img.CopyFile(node, o, n => Progress(n, e.path)), keepExisting: true);
                }
                var gotRel = Path.GetRelativePath(outDir, got).Replace('\\', '/');
                if (gotRel != e.path) Console.Error.WriteLine($"[info] {e.path} already exists with other content; extracted as {gotRel}");
                written.Add(gotRel);
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
        // the table only prints at the end; these milestones keep the caller's bar moving
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
            // PlayGo files under swapped names install fine, then die at launch with CE-100022-5
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
            // the library's own whole-set check; a set copied from another package fails here
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

        // read the inner PFS in place: the file table and eboot.bin, never the whole game
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
                // 0xEEF51454 = fake-self (MakeFself, Sony SELFs); 0x1D3D154F = PS4 SELF, won't load on PS5
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

    // sce_sys checks beyond presence; the retail reference package passes them all
    static void ValidateSystemEntries(string cnt, string contentId, Action<string, string> Ok, Action<string, string> Warn, Action<string, string> Fail)
    {
        string P(string n) => Path.Combine(cnt, n);

        // 8-bit RGB (icon0/pic0/pic1) or RGBA (pic2); icon0 512x512 needs its BC7 dds or nothing starts
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

        // the builder caps it at 9.00 and floors it at the SDK; older consoles refuse to start
        if (root.TryGetProperty("requiredSystemSoftwareVersion", out var rs) && rs.GetString() is string rss)
        {
            var fw = FirmwareText(rss);
            var sdk = root.TryGetProperty("sdkVersion", out var sv) && sv.GetString() is string svs ? FirmwareText(svs) : null;
            if (fw == null) Warn("param.firmware", $"requiredSystemSoftwareVersion '{rss}' is not 0x + 16 hex digits");
            else Ok("param.firmware", $"requires system software {fw}" + (sdk != null ? $" (SDK {sdk})" : ""));
        }
    }

    // 0x0910000000000000 -> 9.10; null if not 16 hex digits
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
            Console.WriteLine($"  {tag}  {r.Check,-24}  {SafeLineWriter.Escape(r.Message)}");   // a message may name a file
        }
        Console.WriteLine();
        Console.WriteLine($"summary: {p} passed, {w} warned, {f} failed");
    }

    static bool IsInside(string path, string root)
    {
        var p = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        var r = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        return p.Equals(r, StringComparison.Ordinal) || p.StartsWith(r + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }

    // case-insensitive on Windows and macOS
    static StringComparison FsComparison =>
        OperatingSystem.IsWindows() || OperatingSystem.IsMacOS() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;

    // below root after resolving links, and nothing below root is a link itself
    static bool IsRealFileInside(string path, string root)
    {
        var r = Path.TrimEndingDirectorySeparator(RealPath(root));
        var p = RealPath(path);
        if (!p.StartsWith(r + Path.DirectorySeparatorChar, FsComparison)) return false;
        var rootFull = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        var full = Path.GetFullPath(path);
        if (!full.StartsWith(rootFull + Path.DirectorySeparatorChar, FsComparison)) return false;
        for (var cur = full; cur.Length > rootFull.Length; cur = Path.GetDirectoryName(cur)!)
        {
            try
            {
                FileSystemInfo info = Directory.Exists(cur) ? new DirectoryInfo(cur) : new FileInfo(cur);
                if (info.LinkTarget != null || info.Attributes.HasFlag(FileAttributes.ReparsePoint)) return false;
            }
            catch { return false; }
        }
        return true;
    }

    // ELF64 with headers or segments past EOF: the signer would zero-fill the gap
    static string? TruncatedSegment(byte[] b)
    {
        if (b.Length < 0x40 || b[4] != 2) return null;            // not ELF64: the signer decides
        ulong phoff = BitConverter.ToUInt64(b, 0x20);
        int phentsize = BitConverter.ToUInt16(b, 0x36), phnum = BitConverter.ToUInt16(b, 0x38);
        if (phnum == 0) return null;
        if (phentsize < 0x38 || phoff + (ulong)phentsize * (ulong)phnum > (ulong)b.Length)
            return "program headers";
        for (int i = 0; i < phnum; i++)
        {
            int at = (int)phoff + i * phentsize;
            ulong off = BitConverter.ToUInt64(b, at + 0x08), size = BitConverter.ToUInt64(b, at + 0x20);
            if (size > 0 && (off > (ulong)b.Length || size > (ulong)b.Length - off))
                return $"segment {i}: {off + size} of {b.Length} bytes";
        }
        return null;
    }

    static void MakeWritable(string path)
    {
        if (OperatingSystem.IsWindows())
            File.SetAttributes(path, File.GetAttributes(path) & ~FileAttributes.ReadOnly);
        else
            File.SetUnixFileMode(path, File.GetUnixFileMode(path) | UnixFileMode.UserWrite);
    }

    static void ReplaceFile(string path, Action<string> writeTemp)
    {
        var full = Path.GetFullPath(path);
        var tmp = Path.Combine(Path.GetDirectoryName(full)!, "." + Path.GetFileName(full) + "." + Guid.NewGuid().ToString("N")[..8] + ".tmp");
        try
        {
            writeTemp(tmp);
            if (OperatingSystem.IsWindows() && File.Exists(full)) File.SetAttributes(full, FileAttributes.Normal);
            File.Move(tmp, full, overwrite: true);
        }
        finally
        {
            try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
        }
    }

    static bool Occupied(string p)
    {
        try { return File.Exists(p) || Directory.Exists(p) || new FileInfo(p).LinkTarget != null; } catch { return true; }
    }

    static bool SameContent(string a, string b)
    {
        using var fa = new FileStream(a, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20);
        using var fb = new FileStream(b, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20);
        if (fa.Length != fb.Length) return false;
        var ba = new byte[1 << 20]; var bb = new byte[1 << 20];
        while (true)
        {
            int n = fa.ReadAtLeast(ba, ba.Length, throwOnEndOfStream: false);
            if (n == 0) return true;
            if (fb.ReadAtLeast(bb, n, throwOnEndOfStream: false) != n || !ba.AsSpan(0, n).SequenceEqual(bb.AsSpan(0, n))) return false;
        }
    }

    // temp name first, then rename; with keepExisting a different file stays, the new one gets (2)
    static string WriteExtractedFile(string dst, Action<Stream> write, bool keepExisting)
    {
        var dir = Path.GetDirectoryName(dst)!;
        var tmp = Path.Combine(dir, "." + Path.GetFileName(dst) + "." + Guid.NewGuid().ToString("N")[..8] + ".part");
        try
        {
            using (var fs = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 20))
                write(fs);
            if (!keepExisting)
            {
                File.Move(tmp, dst, overwrite: true);
                return dst;
            }
            var stem = Path.GetFileNameWithoutExtension(dst);
            var ext = Path.GetExtension(dst);
            for (int n = 1; n < 10000; n++)
            {
                var target = n == 1 ? dst : Path.Combine(dir, $"{stem} ({n}){ext}");
                if (Occupied(target))
                {
                    if (File.Exists(target) && new FileInfo(target).LinkTarget == null && SameContent(tmp, target)) return target;
                    continue;
                }
                try
                {
                    File.Move(tmp, target, overwrite: false);
                    return target;
                }
                catch (IOException) when (Occupied(target)) { }   // appeared meanwhile: next name
            }
            throw new IOException("no free name next to " + dst);
        }
        finally
        {
            try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
        }
    }

    // real copies, never hard links: every pre-build change would reach the user's source
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

    // bundle leftovers under ~/.net are ours to delete; skipped while another copy runs
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

    // a single-file exe sets up no probing path, so find Magick.Native ourselves next to the exe
    static bool _magickRedirected;
    static void RedirectMagickNative()
    {
        if (_magickRedirected) return;
        _magickRedirected = true;
        bool trace = Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1";
        var baseDir = AppContext.BaseDirectory ?? Environment.CurrentDirectory;
        // probe only this build's own dirs, never old ~/.net extractions or TMPDIR
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
        // every Magick assembly has its own DllImport stubs, hook them all
        int hooked = 0;
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            var n = asm.GetName().Name ?? "";
            if (!n.StartsWith("Magick", StringComparison.OrdinalIgnoreCase)) continue;
            try { NativeLibrary.SetDllImportResolver(asm, MagickResolver); hooked++; if (trace) Console.Error.WriteLine("[trace] Magick resolver hooked on: " + n); }
            catch (Exception ex) { if (trace) Console.Error.WriteLine("[trace] Magick resolver skip " + n + ": " + ex.Message); }
        }
        // Magick.NET loads more assemblies later, hook those too
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
        bool playGoChunksExplicit = false;
        bool retailNormalize = true;  // make a standard retail source console-launchable
        bool regenPlayGo = false;         // force-discard a prepared PlayGo set even if it validates
        string? keystoneFile = null;      // --keystone: the save-data key of the game the saves come from
        bool amprIndex = true;            // rebuild ampr_emu.index when the AMPR emulator is shipped
        string hdrFlag = "auto";  // attribute bit 29 (HDR): auto, on or off

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
            // 0 = one worker per core, for Kraken and the outer PFS. the multi-worker crashes were
            // the compressed single-file bundle, keep EnableCompressionInSingleFile off
            KrakenMaxDegreeOfParallelism = 0,
            LegacyZlibMaxDegreeOfParallelism = 0,
            // the PS5 debug loader only takes the plaintext-no-auth outer PFS (0x000D); an AES-XTS
            // wrap installs fine and then fails with CE-100096-6. Sony's tools do the same
            PublisherImageMode = ProsperoPublisherImageMode.PlaintextNoAuth,
            // Sony puts every launch-time file in chunk 0; the source's playgo-chunk.dat overrides
            PlayGoChunkCount = 1,
            // the source's set is never copied verbatim; the library rebuilds its ranges for this image
            PreserveSourcePlayGoLayout = true,
            // layout pass only rewrites when it saves >= 1 MiB and 0.1 %; --layout-pass off skips it
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
                    stageInPlace = true;
                    break;
                case "--consume-source":
                    // only with --stage-in-place: each packed file is deleted; the caller unpacks again on failure
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
                        // stored and automatic install fine but fail at launch with CE-100096-6; only builtin works
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
                    // only for a generated set: Sony packs everything into chunk 0; a source playgo-chunk.dat wins
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int chunks) || chunks < 1 || chunks > 255)
                        throw new ArgumentException("--playgo-chunks needs an integer 1..255 (LibProsperoPkg's range)");
                    opts.PlayGoChunkCount = chunks;
                    playGoChunksExplicit = true;
                    break;
                }
                case "--no-fake-sign":
                    // retail ELFs must be fake-signed to boot; skip only for already-signed or exotic SELFs
                    autoFakeSign = false;
                    break;
                case "--fake-sign":
                    autoFakeSign = true;
                    break;
                case "--no-retail-normalize":
                    // swaps placeholder licenses for a debug one and stamps retail SELFs; off for byte-exact repacks
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
                case "--keystone": keystoneFile = Need(args, ref i, a); break;
                case "--regen-playgo":
                    regenPlayGo = true;
                    break;
                case "--no-ampr-index":
                    amprIndex = false;
                    break;
                case "--hdr-flag":
                    // auto keeps the source's bit 29, on sets it, off clears it
                    hdrFlag = Need(args, ref i, a).ToLowerInvariant();
                    if (hdrFlag is not ("auto" or "on" or "off"))
                        throw new ArgumentException("--hdr-flag needs auto, on or off");
                    break;
                case "--no-hdr-flag": hdrFlag = "off"; break;  // old spelling
                default: return Bad("unknown build flag: " + a);
            }
        }
        if (keystoneFile != null)
        {
            var ks = File.Exists(keystoneFile) ? File.ReadAllBytes(keystoneFile) : null;
            if (ks == null || ks.Length != 96 || System.Text.Encoding.ASCII.GetString(ks, 0, 8) != "keystone")
                return Bad("--keystone needs a keystone file (96 bytes, starting with 'keystone'): " + keystoneFile);
        }
        if (string.IsNullOrWhiteSpace(opts.ContentId)) return Bad("--content-id required");
        if (string.IsNullOrWhiteSpace(opts.TitleId)) return Bad("--title-id required");
        Directory.CreateDirectory(opts.OutputFolder);

        // always build from a stage: the library writes a param.json into its build folder
        // and that must never be the user's source. real copies, never hard links
        string effectiveSource = opts.SourceFolder!;
        string? autoStage = null;
        var srcSceSys = Path.Combine(effectiveSource, "sce_sys");

        // PlayGo chunk count from the source, unless pinned
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
                    // playgo-chunk.dat: 0x00 'plgx', 0x08 u16 attribute count, 0x0A u16 chunk count
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

        // standard-DRM retail dumps need drm_type 16, real license entries and retail SELFs,
        // all done on the stage, never the source. what actually broke launches was PlayGo
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
            var elfsToSign = autoFakeSign ? FindRawElfs(effectiveSource) : Array.Empty<string>();

            // mislabeled PlayGo files die at launch (CE-100022-5); one bad file drops the whole set
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

            // non-JSON sce_sys/*.json is a mislabeled dump leftover, drop it
            var badJson = Directory.Exists(srcSceSys)
                ? Directory.EnumerateFiles(srcSceSys, "*.json").Where(p =>
                    { try { return !LooksLikeJsonText(File.ReadAllBytes(p)); } catch { return false; } })
                  .Select(p => Path.GetFileName(p)!).ToArray()
                : Array.Empty<string>();
            // a broken param.json would become a generic one and lose the title's settings. refuse
            if (badJson.Contains("param.json", StringComparer.OrdinalIgnoreCase))
                throw new InvalidDataException("sce_sys/param.json is not valid JSON (a damaged or mislabeled dump). "
                    + "A package built now would carry a generic param.json and lose the title's settings. Put the title's "
                    + "real param.json in place, or delete the file to build with a generated one.");
            if (badJson.Length > 0)
                Console.Error.WriteLine($"  [sce_sys] dropping mislabeled non-JSON file(s): {string.Join(", ", badJson)}");

            // an empty eboot.bin can't start, an empty module is a truncated copy
            var ebootSrc = Path.Combine(effectiveSource, "eboot.bin");
            if (File.Exists(ebootSrc) && new FileInfo(ebootSrc).Length < 64)
                throw new InvalidDataException($"eboot.bin is {new FileInfo(ebootSrc).Length} bytes — truncated or empty; nothing was built.");
            foreach (var m in Directory.EnumerateFiles(effectiveSource, "*", SearchOption.AllDirectories)
                         .Where(f => f.EndsWith(".prx", StringComparison.OrdinalIgnoreCase) || f.EndsWith(".sprx", StringComparison.OrdinalIgnoreCase))
                         .Where(f => { try { return new FileInfo(f).Length == 0; } catch { return false; } }))
                Console.Error.WriteLine($"[warn] {Path.GetRelativePath(effectiveSource, m).Replace('\\', '/')} is an empty file (truncated copy?); a game that loads it will crash");

            // a regenerated keystone makes every existing save look corrupt; the library keeps a present one
            if (keystoneFile != null)
                Console.Error.WriteLine($"  [keystone] replaced with {keystoneFile} (saves of the game it came from stay readable)");
            else if (File.Exists(Path.Combine(srcSceSys, "keystone")))
                Console.Error.WriteLine("  [keystone] source keystone preserved (save-data key; saves stay compatible with the original dump)");
            else
                Console.Error.WriteLine($"  [keystone] source has no sce_sys/keystone — the builder generates one for passcode {opts.Passcode}; saves from other builds of this title will NOT be readable");

            // every failure here is fatal: a raw-source package misses license, signing, retail bit
            // except the DDS conversion, it runs last and is caught per icon
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
                    // a real copy, never hard links (see MirrorSource)
                    autoStage = Path.Combine(tempRoot, "ffpfsc-stage-" + Guid.NewGuid().ToString("N").Substring(0, 8));
                    Directory.CreateDirectory(autoStage);
                    long stageBytes = DirectorySize(effectiveSource);
                    Console.Error.WriteLine($"[stage] copying {stageBytes / 1073741824.0:F1} GB into {autoStage} (the source stays untouched)");
                    MirrorSource(effectiveSource, autoStage, stageBytes);
                    Console.Error.WriteLine($"  [stage] mirrored source into {autoStage} (copy)");
                }

                if (keystoneFile != null)
                {
                    Directory.CreateDirectory(Path.Combine(autoStage, "sce_sys"));
                    File.Copy(keystoneFile, Path.Combine(autoStage, "sce_sys", "keystone"), true);
                }

                // a corrupt PlayGo file is CE-100022-5 at launch, so a failed unlink is fatal
                if (discardPlayGo || badJson.Length > 0)
                {
                    var stagedSceSys0 = Path.Combine(autoStage, "sce_sys");
                    foreach (var n in (discardPlayGo ? playgoPresent : Array.Empty<string>()).Concat(badJson))
                    {
                        var p = Path.Combine(stagedSceSys0, n);
                        if (File.Exists(p) || IsSymlink(p)) File.Delete(p);
                    }
                }

                // fake-sign raw ELFs with LibProsperoPkg's MakeFself; SELFs and non-ELFs skipped
                if (elfsToSign.Length > 0)
                {
                    int signed = 0, skipped = 0;
                    foreach (var relPath in elfsToSign)
                    {
                        var srcFile = Path.Combine(effectiveSource, relPath);
                        var stagedFile = Path.Combine(autoStage, relPath);
                        byte[] fself;
                        var bytes = File.ReadAllBytes(srcFile);
                        if (!ProsperoFself.IsElf(bytes) || ProsperoFself.IsSelf(bytes)) { skipped++; continue; }
                        var cut = TruncatedSegment(bytes);
                        if (cut != null)
                            throw new InvalidOperationException($"{relPath} is cut short ({cut}); the game copy is incomplete");
                        try
                        {
                            fself = ProsperoFself.MakeFself(bytes, new FselfOptions());
                        }
                        catch (Exception ex) when (ex is ArgumentException || ex is InvalidDataException || ex is FormatException)
                        {
                            // a 32-bit or odd ELF the signer won't take: leave it alone
                            Console.Error.WriteLine($"  [sign] skipped {relPath}: {ex.Message}");
                            skipped++;
                            continue;
                        }
                        try
                        {
                            // swap in by rename; in place this is the source file itself
                            Directory.CreateDirectory(Path.GetDirectoryName(stagedFile)!);
                            ReplaceFile(stagedFile, tmp => File.WriteAllBytes(tmp, fself));
                        }
                        catch (Exception ex)
                        {
                            // the signed module could not be written: its package would not start
                            throw new InvalidOperationException($"fake-signing {relPath} failed ({ex.GetType().Name}: {ex.Message})", ex);
                        }
                        signed++;
                    }
                    Console.Error.WriteLine($"  [sign] fake-signed {signed} ELF(s); skipped {skipped} (already-SELF, non-ELF, or not a signable module)");
                }

                // debug license as CNT 0x0400/0x0401; drm_type=16 comes from the patched dll
                // keep drmType standard: upgradable plus the stripped upgrade chain is CE-100022-5
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

                    // retail bit is byte 0x0B (0x00 to 0x10) only; touching other header bytes confuses the loader
                    int selfPatched = 0, selfLeft = 0;
                    foreach (var relPath in Directory.EnumerateFiles(autoStage, "*", SearchOption.AllDirectories).ToList())
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
                            // patch a copy and rename it over: a hard link to the source is never written through
                            ReplaceFile(relPath, tmp =>
                            {
                                File.Copy(relPath, tmp, overwrite: true);
                                MakeWritable(tmp);  // the copy inherits read-only mode
                                using var fs = new FileStream(tmp, FileMode.Open, FileAccess.Write);
                                fs.Seek(0x0B, SeekOrigin.Begin);
                                fs.WriteByte(0x10);
                            });
                            selfPatched++;
                        }
                        catch (Exception ex)
                        {
                            throw new InvalidOperationException($"setting the retail bit on {relFromRoot} failed ({ex.GetType().Name}: {ex.Message})", ex);
                        }
                    }
                    Console.Error.WriteLine($"  [self-hdr] set retail bit (byte 0x0B: 0x10) on {selfPatched} eboot.bin/*.prx; left {selfLeft} untouched");

                    RewriteStagedParamJson(srcSceSys, stagedSceSys, hdrFlag, addKernel: true, tag: "retail");
                    Console.Error.WriteLine("  [retail] dropped placeholder license.dat/info; installed DebugLicenseProvider (CNT entries 0x0400/0x0401); drm_type=16 via patched LibProsperoPkg.dll");
                }

                if (!doRetailNormalize && hdrFlag != "auto")
                    RewriteStagedParamJson(srcSceSys, Path.Combine(autoStage!, "sce_sys"), hdrFlag, addKernel: false, tag: "param");

                // icon0/pic0/pic1 must be 8-bit RGB, pic2 8-bit RGBA (see PresentationMedia.cs)
                PresentationMedia.Normalize(Path.Combine(autoStage, "sce_sys"), m => Console.Error.WriteLine(m));

                // DDS errors are caught per icon; without icon0.dds it installs but never launches
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
                            var stagedPng = Path.Combine(stagedSceSys, name);
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

                // the AMPR emulator resolves APR ids via /app0/ampr_emu.index; signing changes sizes, so rebuild
                if (amprIndex && File.Exists(Path.Combine(autoStage, "fakelib", "libSceAmpr.sprx")))
                {
                    // packed files carry the build TimeStamp as inode time (epoch unless set)
                    long pkgTime = (long)opts.TimeStamp.ToUniversalTime().Subtract(DateTime.UnixEpoch).TotalSeconds;
                    // the builder rewrites param.json while packing; do it now so the index sees the real size
                    PublishParamJson(autoStage, opts);
                    int rows = AmprIndex.Write(autoStage, pkgTime, packageView: true);
                    Console.Error.WriteLine(rows > 0
                        ? $"  [ampr] rebuilt ampr_emu.index over the packed files ({rows:N0} file(s))"
                        : "  [ampr] nothing to index; ampr_emu.index left as it is");
                }

                // a PlayGo set made for another file tree lies; let the builder regenerate it on mismatch
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

                // no param.json means the builder writes drm standard, which needs a license too
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

        // cleanup removes only what this build wrote; a .pkg the user drops in meanwhile stays
        string outFull = Path.GetFullPath(opts.OutputFolder);
        string tempFull = Path.GetFullPath(string.IsNullOrWhiteSpace(opts.TemporaryDirectory) ? Path.GetTempPath() : opts.TemporaryDirectory!);
        string? pkgName = PackageFileName(opts);
        var pkgBefore = new Dictionary<string, (long, DateTime)>(StringComparer.Ordinal);
        foreach (var p in SafeEnumerate(outFull, "*.pkg")) pkgBefore[p] = FileStamp(p);
        bool OursPkg(string p)
        {
            var n = Path.GetFileName(p);
            return pkgName != null ? n.Equals(pkgName, FsComparison)
                                   : n.StartsWith(opts.ContentId + "-", FsComparison);   // name unknown: this content id only
        }
        IEnumerable<string> BuildTempFiles() => pkgName == null ? Enumerable.Empty<string>()
            : SafeEnumerate(outFull, "." + pkgName + ".*.tmp").Concat(SafeEnumerate(tempFull, "." + pkgName + ".*.tmp"));
        foreach (var p in BuildTempFiles().Distinct().ToList()) TryDeleteFile(p, "stale build temp file");
        var tempBefore = new HashSet<string>(LibraryTempFiles(tempFull), StringComparer.Ordinal);
        void CleanupAfterFailure()
        {
            if (!stageInPlace) CleanupStage(autoStage);
            foreach (var p in SafeEnumerate(outFull, "*.pkg"))
                if (OursPkg(p) && (!pkgBefore.TryGetValue(p, out var was) || was != FileStamp(p))) TryDeleteFile(p, "partial package");
            foreach (var p in BuildTempFiles().Distinct().ToList()) TryDeleteFile(p, "build temp file");
            foreach (var p in LibraryTempFiles(tempFull)) if (!tempBefore.Contains(p)) TryDeleteFile(p, "library temp file");
        }

        // the GUI sends SIGKILL 5 s after SIGTERM, so stop waiting for the build after 2.5 s
        using var cts = new CancellationTokenSource();
        opts.CancellationToken = cts.Token;
        int signalExit = 0;
        using var buildDone = new ManualResetEventSlim(false);
        void OnSignal(PosixSignalContext ctx)
        {
            ctx.Cancel = true;  // no runtime exit, cleanup comes first
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
            // e.g. 'data  53% (1,234/2,000): /a file.bin -> ...'; the name ends at the last ' -> '
            var packedLine = new System.Text.RegularExpressions.Regex(@"\[inner\]\s+data\s+\d+%\s+\([\d.,'\u00A0\u202F ]+/[\d.,'\u00A0\u202F ]+\):\s+(/.+) -> ");
            void Log(string s)
            {
                Console.Error.WriteLine("  " + s);
                if (!(consumeSource && stageInPlace && autoStage != null)) return;
                var m = packedLine.Match(s);
                if (!m.Success) return;
                // a file named here is packed and never read again, so it can go
                // never sce_sys or anything through a link: param.json and media are read again later
                var rel = m.Groups[1].Value.TrimStart('/').Replace('/', Path.DirectorySeparatorChar);
                var first = rel.Split(Path.DirectorySeparatorChar)[0];
                if (first.Equals("sce_sys", StringComparison.OrdinalIgnoreCase)) return;
                var f = Path.GetFullPath(Path.Combine(autoStage, rel));
                if (!File.Exists(f) || !IsRealFileInside(f, autoStage)) return;
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
        if (pkgName != null && !Path.GetFileName(result.OutputPath).Equals(pkgName, FsComparison))
            Console.Error.WriteLine($"[warn] the library named the package {Path.GetFileName(result.OutputPath)}, not {pkgName}: a failed build of this name may leave a partial file");
        Console.WriteLine($"OK — wrote {result.OutputPath} ({new FileInfo(result.OutputPath).Length:N0} B)");
        if (result.Warnings != null)
            foreach (var w in result.Warnings) Console.Error.WriteLine("[warn] " + w);
        return 0;
    }

    // HDR bit 29 per --hdr-flag (auto keeps the source's) plus a kernel block when missing
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
        bool addAttr = !hasAttribute && newAttr != currentAttr;
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
                // keep source order; Sony puts kernel just before localizedParameters
                if (wantKernel && !kernelWritten && prop.Name == "localizedParameters") { WriteKernel(); kernelWritten = true; }
                if (prop.Name == "attribute") { w.WriteNumber("attribute", newAttr); attrWritten = true; }
                else prop.WriteTo(w);
                // Sony's order: attribute right after applicationDrmType
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

    static IEnumerable<string> LibraryTempFiles(string tempDir)
        => SafeEnumerate(tempDir, "libprospero-publisher-*").Concat(SafeEnumerate(tempDir, ".*.cnt.tmp"));

    static (long, DateTime) FileStamp(string path)
    {
        try { var fi = new FileInfo(path); return (fi.Length, fi.LastWriteTimeUtc); } catch { return (-1, DateTime.MinValue); }
    }

    // null once the library drops its private ComposePkgFileName
    static string? PackageFileName(ProsperoBuildOptions opts)
    {
        try
        {
            var m = typeof(ProsperoPackageBuilder).GetMethod("ComposePkgFileName", BindingFlags.NonPublic | BindingFlags.Static,
                                                            new[] { typeof(string), typeof(string) });
            return m?.Invoke(null, new object[] { opts.ContentId, opts.Version }) as string;
        }
        catch { return null; }
    }

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

    // best effort: replays the builder's private param.json rewrite, warns when it can't
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

    // the library's inner image: no .gp4/.gp5, decrypted/, ext_info.dat; right.sprx added
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

    // why the PlayGo set doesn't fit these paths (null = fine); same checks as ReadSourceLayout
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

    // the library's own BuildDebugLicense, handed out as CNT 0x0400/0x0401
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

    // PlayGo formats as LibProsperoPkg writes them, same as Sony's publisher
    static uint U32(byte[] b, int o) => (uint)(b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24));

    // playgo-chunk.dat: plgx magic, total size at 0x10
    static bool LooksLikePlayGoChunkDat(byte[] b)
        => b.Length >= 0x40 && b[0] == (byte)'p' && b[1] == (byte)'l' && b[2] == (byte)'g' && b[3] == (byte)'x'
           && U32(b, 0x10) == (uint)b.Length;

    // playgo-hash-table.dat: 56-byte header, then the table
    static bool LooksLikePlayGoHashTable(byte[] b)
        => b.Length >= 56 && U32(b, 0) == 1u && U32(b, 4) == 0x08000000u && U32(b, 8) == 56u
           && b[24] == 0x7F && b[25] == (byte)'F' && b[26] == (byte)'L' && b[27] == (byte)'T'
           && 56u + U32(b, 12) == (uint)b.Length;

    // playgo-ficm.dat: 16-byte header, then the payload
    static bool LooksLikePlayGoFicm(byte[] b)
        => b.Length >= 16 && U32(b, 0) == 1u && U32(b, 8) == 16u && 16u + U32(b, 12) == (uint)b.Length;

    // LibProsperoPkg refuses symlinks anywhere in the target path (/tmp -> /private/tmp, thanks macOS)
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

    // only eboot.bin/.elf/.prx/.sprx get sniffed, no point reading huge blobs
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
    // pretty-printed, for the human-facing --json output
    public static PkgToolJsonContext Indented => _indented ??= new PkgToolJsonContext(new JsonSerializerOptions { WriteIndented = true });
}
