using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using BCnEncoder.Decoder;
using BCnEncoder.Shared.ImageFiles;
using ImageMagick;

namespace PkgTool;

// sce_sys PNGs must be 8-bit, not interlaced, RGB (pic2 RGBA), icon0 512x512
// replace by delete + write: never write through a hard link into the user's source
internal static class PresentationMedia
{
    public const int ColourRgb = 2, ColourRgba = 6;
    public const int IconSize = 512;
    public const int PicWidth = 3840, PicHeight = 2160;

    public readonly record struct PngInfo(int Width, int Height, int Depth, int Colour, int Interlace);

    // icon0, pic0, pic1 or pic2 for a PNG name (also _NN variants), else null
    public static string? Kind(string pngName)
    {
        var stem = Path.GetFileNameWithoutExtension(pngName).ToLowerInvariant();
        var baseName = stem.Length > 3 && stem[^3] == '_' && char.IsDigit(stem[^2]) && char.IsDigit(stem[^1])
            ? stem[..^3] : stem;
        return baseName is "icon0" or "pic0" or "pic1" or "pic2" ? baseName : null;
    }

    public static int? RequiredColour(string pngName) => Kind(pngName) switch
    {
        "icon0" or "pic0" or "pic1" => ColourRgb,
        "pic2" => ColourRgba,
        _ => null,
    };

    // IHDR fields of a PNG, else null
    public static PngInfo? ReadPng(ReadOnlySpan<byte> h)
    {
        if (h.Length < 29) return null;
        ReadOnlySpan<byte> sig = stackalloc byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A };
        if (!h[..8].SequenceEqual(sig)) return null;
        if (h[12] != (byte)'I' || h[13] != (byte)'H' || h[14] != (byte)'D' || h[15] != (byte)'R') return null;
        int w = (int)BinaryPrimitives.ReadUInt32BigEndian(h[16..]), ht = (int)BinaryPrimitives.ReadUInt32BigEndian(h[20..]);
        if (w <= 0 || ht <= 0) return null;
        return new PngInfo(w, ht, h[24], h[25], h[28]);
    }

    public static PngInfo? ReadPng(string path)
    {
        try
        {
            using var f = File.OpenRead(path);
            var h = new byte[29];
            return f.Read(h, 0, 29) == 29 ? ReadPng(h) : null;
        }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
    }

    // null when valid; size is enforced for icon0 only, other pics just get a SizeNote
    public static string? Problem(string pngName, PngInfo? info)
    {
        if (info is not PngInfo p) return "not a PNG";
        int want = RequiredColour(pngName)!.Value;
        var issues = new List<string>();
        if (p.Depth != 8) issues.Add($"{p.Depth}-bit");
        if (p.Colour != want) issues.Add($"colour type {p.Colour}, needs {(want == ColourRgb ? "RGB" : "RGBA")}");
        if (p.Interlace != 0) issues.Add("interlaced");
        if (Kind(pngName) == "icon0" && (p.Width != IconSize || p.Height != IconSize))
            issues.Add($"{p.Width}x{p.Height}, needs {IconSize}x{IconSize}");
        return issues.Count == 0 ? null : string.Join(", ", issues);
    }

    public static string? SizeNote(string pngName, PngInfo? info)
    {
        if (info is not PngInfo p || Kind(pngName) is not ("pic0" or "pic1" or "pic2")) return null;
        if (p.Width == PicWidth && p.Height == PicHeight) return null;
        return $"sce_sys/{pngName} is {p.Width}x{p.Height}; retail packages use {PicWidth}x{PicHeight}"
            + (p.Width % 4 != 0 || p.Height % 4 != 0 ? " (and a DDS needs sides that are multiples of 4)" : "");
    }

    static byte[] Encode(MagickImage img, string pngName)
    {
        int colour = RequiredColour(pngName)!.Value;
        img.Strip();                             // no profiles, comments or EXIF
        img.Depth = 8;
        if (Kind(pngName) == "icon0" && (img.Width != IconSize || img.Height != IconSize))
            img.Resize(new MagickGeometry(IconSize, IconSize) { IgnoreAspectRatio = true });
        if (colour == ColourRgb)
        {
            // flatten onto black: colour under transparent pixels is often junk
            if (img.HasAlpha)
            {
                img.BackgroundColor = MagickColors.Black;
                img.Alpha(AlphaOption.Remove);
            }
            img.Alpha(AlphaOption.Off);
            img.Format = MagickFormat.Png24;
        }
        else
        {
            img.Alpha(AlphaOption.Set);          // opaque alpha when the source had none
            img.Format = MagickFormat.Png32;
        }
        img.Settings.Interlace = Interlace.NoInterlace;
        // critical chunks only: ImageMagick stamps the wall clock and breaks reproducible builds
        img.Settings.SetDefine(MagickFormat.Png, "exclude-chunk", "all");
        var png = img.ToByteArray();
        var problem = Problem(pngName, ReadPng(png));
        if (problem != null) throw new InvalidDataException("encoder output is still wrong: " + problem);
        return png;
    }

    static byte[] PngFromDds(string ddsPath, string pngName)
    {
        using var fs = File.OpenRead(ddsPath);
        var dds = DdsFile.Load(fs);
        var pixels = new BcDecoder().Decode(dds);
        int width = (int)dds.header.dwWidth, height = (int)dds.header.dwHeight;
        var rgba = MemoryMarshal.AsBytes(pixels.AsSpan()).ToArray();
        if (rgba.Length != width * height * 4)
            throw new InvalidDataException($"decoded {rgba.Length} bytes for {width}x{height}");
        var settings = new PixelReadSettings((uint)width, (uint)height, StorageType.Char, PixelMapping.RGBA);
        using var img = new MagickImage();
        img.ReadPixels(rgba, settings);
        return Encode(img, pngName);
    }

    static void Replace(string path, byte[] data)
    {
        if (File.Exists(path)) File.Delete(path);   // breaks a hard link instead of writing through it
        File.WriteAllBytes(path, data);
    }

    // runs on the staged mirror; every change is logged
    public static int Normalize(string sceSys, Action<string> log)
    {
        if (!Directory.Exists(sceSys)) return 0;
        var names = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var f in Directory.EnumerateFiles(sceSys))
        {
            var n = Path.GetFileName(f);
            var ext = Path.GetExtension(n).ToLowerInvariant();
            if ((ext == ".png" || ext == ".dds") && Kind(n) != null)
                names.Add(Path.ChangeExtension(n, ".png"));
        }
        int changed = 0;
        foreach (var pngName in names)
        {
            string mode = RequiredColour(pngName) == ColourRgb ? "RGB" : "RGBA";
            string png = Path.Combine(sceSys, pngName);
            string dds = Path.ChangeExtension(png, ".dds");
            bool hasPng = File.Exists(png), hasDds = File.Exists(dds);
            var info = hasPng ? ReadPng(png) : null;
            string? problem = hasPng ? Problem(pngName, info) : "missing";
            if (problem == null)
            {
                if (SizeNote(pngName, info) is string note) log("[warn] " + note);
                continue;
            }
            try
            {
                if (info != null)
                {
                    using var img = new MagickImage(File.ReadAllBytes(png));
                    Replace(png, Encode(img, pngName));
                    log($"  [media] converted sce_sys/{pngName} to 8-bit {mode} ({problem})");
                    changed++;
                }
                else if (hasDds)
                {
                    Replace(png, PngFromDds(dds, pngName));
                    log($"  [media] regenerated sce_sys/{pngName} from {Path.GetFileName(dds)} ({problem}; 8-bit {mode})");
                    changed++;
                }
                else
                {
                    File.Delete(png);
                    log($"[warn] sce_sys/{pngName} is not a PNG and has no .dds to recover from; left out of the package"
                        + (Kind(pngName) == "icon0" ? " — without an icon the package will not launch on a console" : ""));
                    changed++;
                }
                if (SizeNote(pngName, ReadPng(png)) is string note) log("[warn] " + note);
            }
            catch (Exception ex) when (ex is IOException or InvalidDataException or MagickException or InvalidOperationException or NotSupportedException or ArgumentException)
            {
                log($"[warn] sce_sys/{pngName}: could not repair ({problem}): {ex.GetType().Name}: {ex.Message}");
            }
        }
        return changed;
    }

    // null when fine: DX10 header, BC7 (DXGI 98/99), the PNG's size, one full mip
    public static string? DdsProblem(string ddsPath, PngInfo? png)
    {
        byte[] h;
        long len;
        try
        {
            using var f = File.OpenRead(ddsPath);
            len = f.Length;
            h = new byte[148];
            if (f.Read(h, 0, 148) != 148) return "shorter than a DX10 header";
        }
        catch (IOException ex) { return "unreadable: " + ex.Message; }
        catch (UnauthorizedAccessException ex) { return "unreadable: " + ex.Message; }
        if (h[0] != (byte)'D' || h[1] != (byte)'D' || h[2] != (byte)'S' || h[3] != (byte)' ') return "not a DDS";
        if (h[84] != (byte)'D' || h[85] != (byte)'X' || h[86] != (byte)'1' || h[87] != (byte)'0') return "no DX10 header";
        uint dxgi = BinaryPrimitives.ReadUInt32LittleEndian(h.AsSpan(128));
        if (dxgi != 98 && dxgi != 99) return $"DXGI format {dxgi}, needs BC7 (98)";
        int height = (int)BinaryPrimitives.ReadUInt32LittleEndian(h.AsSpan(12));
        int width = (int)BinaryPrimitives.ReadUInt32LittleEndian(h.AsSpan(16));
        if (png is PngInfo p && (p.Width != width || p.Height != height))
            return $"{width}x{height}, the PNG is {p.Width}x{p.Height}";
        long need = 148L + (long)((width + 3) / 4) * ((height + 3) / 4) * 16;
        if (len < need) return $"{len:N0} bytes, one BC7 level needs {need:N0}";
        return null;
    }
}
