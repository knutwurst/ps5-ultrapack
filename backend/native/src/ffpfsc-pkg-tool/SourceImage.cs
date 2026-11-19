// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
// Additional terms under section 7 of the GPL apply, see the NOTICE file.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using LibProsperoPkg.PFS;
using LibProsperoPkg.Util;

namespace PkgTool;

// a .ffpfs/.ffpfsc as a build source without unpacking it: small and special files are written
// out, the rest stay in the image as empty stand-ins whose size and bytes come through SourceHooks
internal sealed class SourceImage : IDisposable
{
    // below this a file is simply written out; the stand-ins only pay off for the big ones
    const long StandInMin = 1 << 20;

    readonly string _image;
    readonly ConcurrentDictionary<string, string> _standIns = new(StringComparer.Ordinal);   // stub path -> image path
    readonly ConcurrentDictionary<string, long> _sizes = new(StringComparer.Ordinal);
    readonly string _rootInImage;

    public string Root { get; }
    public int Written { get; private set; }
    public int StandIns => _standIns.Count;
    public long StandInBytes { get; private set; }
    public long Listed { get; private set; }     // every file the library reads in the image
    public long Compressed { get; private set; } // files stored PFSC-compressed inside the game's PFS

    public static bool IsImage(string path)
        => File.Exists(path) && (path.EndsWith(".ffpfsc", StringComparison.OrdinalIgnoreCase)
                                 || path.EndsWith(".ffpfs", StringComparison.OrdinalIgnoreCase));

    public SourceImage(string image, string stubRoot)
    {
        _image = Path.GetFullPath(image);
        Root = Path.GetFullPath(stubRoot);
        using (var open = OpenInner(_image))
        {
            var files = open.Inner.GetAllFiles().ToList();
            Listed = files.Count;
            // GetView hands out stored bytes: such files would land in the package still compressed
            Compressed = files.LongCount(f => f.flags.HasFlag(InodeFlags.compressed));
            _rootInImage = GameRoot(files.Select(f => Rel(f.FullName)).ToList());
            Directory.CreateDirectory(Root);
            foreach (var f in files.OrderBy(f => f.FullName, StringComparer.Ordinal))
            {
                var rel = Rel(f.FullName);
                if (_rootInImage.Length > 0)
                {
                    if (!rel.StartsWith(_rootInImage + "/", StringComparison.Ordinal)) continue;
                    rel = rel.Substring(_rootInImage.Length + 1);
                }
                if (rel.Length == 0 || FsJunk.PathHasJunk(rel)) continue;
                if (rel.Split('/').Any(p => p is "" or "." or ".."))
                    throw new InvalidDataException("the image names a path that leaves the game folder: " + rel);
                var dst = Path.Combine(Root, rel.Replace('/', Path.DirectorySeparatorChar));
                Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                if (WriteOut(rel, f))
                {
                    using var o = new FileStream(dst, FileMode.CreateNew, FileAccess.Write);
                    Copy(f, o);
                    Written++;
                }
                else
                {
                    using (new FileStream(dst, FileMode.CreateNew, FileAccess.Write)) { }
                    var key = Path.GetFullPath(dst);
                    _standIns[key] = f.FullName;
                    _sizes[key] = f.size;
                    StandInBytes += f.size;
                }
            }
            foreach (var d in AllDirs(open.Inner.GetURoot()))
            {
                var rel = Rel(d.FullName);
                if (_rootInImage.Length > 0)
                {
                    if (!rel.StartsWith(_rootInImage + "/", StringComparison.Ordinal)) continue;
                    rel = rel.Substring(_rootInImage.Length + 1);
                }
                if (rel.Length > 0 && !FsJunk.PathHasJunk(rel) && !rel.Split('/').Any(p => p is "." or ".."))
                    Directory.CreateDirectory(Path.Combine(Root, rel.Replace('/', Path.DirectorySeparatorChar)));
            }
        }
        SourceHooks.Length = path => path != null && _sizes.TryGetValue(Path.GetFullPath(path), out var n) ? n : -1;
        SourceHooks.Open = path => path != null && _standIns.TryGetValue(Path.GetFullPath(path), out var inImage)
            ? OpenFile(inImage) : null;
    }

    // executables, the system and module folders and anything small are real files: the library
    // and the tool read and change those in place (signing, retail fixes, icons, PlayGo)
    bool WriteOut(string rel, PfsReader.File f)
    {
        if (f.size < StandInMin) return true;
        var top = rel.Split('/')[0];
        if (top.Equals("sce_sys", StringComparison.OrdinalIgnoreCase)
            || top.Equals("sce_module", StringComparison.OrdinalIgnoreCase)
            || top.StartsWith("fakelib", StringComparison.OrdinalIgnoreCase)) return true;
        var name = Path.GetFileName(rel);
        if (name.Equals("eboot.bin", StringComparison.Ordinal)) return true;
        var ext = Path.GetExtension(name).ToLowerInvariant();
        if (ext is ".prx" or ".sprx" or ".elf" or ".self") return true;
        // an ELF or SELF under any other name gets signed or stamped: it has to be real
        var head = new byte[4];
        f.GetView().Read(0, head, 0, head.Length);
        return (head[0] == 0x7F && head[1] == (byte)'E' && head[2] == (byte)'L' && head[3] == (byte)'F')
               || (head[0] == 0x4F && head[1] == 0x15 && head[2] == 0x3D && head[3] == 0x1D)
               || (head[0] == 0x54 && head[1] == 0x14 && head[2] == 0xF5 && head[3] == 0xEE);
    }

    static IEnumerable<PfsReader.Dir> AllDirs(PfsReader.Dir d)
    {
        foreach (var c in d.children.OfType<PfsReader.Dir>())
        {
            yield return c;
            foreach (var g in AllDirs(c)) yield return g;
        }
    }

    static string Rel(string fullName)
    {
        var r = fullName.Replace('\\', '/').TrimStart('/');
        return r.StartsWith("uroot/", StringComparison.Ordinal) ? r.Substring(6) : r == "uroot" ? "" : r;
    }

    // an image that holds one game folder: build from inside it
    static string GameRoot(List<string> rels)
    {
        if (rels.Any(r => r.StartsWith("sce_sys/", StringComparison.Ordinal))) return "";
        var tops = rels.Select(r => r.Split('/')[0]).Where(t => !FsJunk.IsJunkName(t)).Distinct().ToList();
        if (tops.Count == 1 && rels.Any(r => r.StartsWith(tops[0] + "/sce_sys/", StringComparison.Ordinal))) return tops[0];
        return "";
    }

    static void Copy(PfsReader.File f, Stream o)
    {
        var view = f.GetView();
        var buf = new byte[1 << 20];
        long pos = 0;
        while (pos < f.size)
        {
            int n = (int)Math.Min(buf.Length, f.size - pos);
            view.Read(pos, buf, 0, n);
            o.Write(buf, 0, n);
            pos += n;
        }
    }

    // every open gets its own handle and readers: the library may read several files at once
    Stream OpenFile(string inImage)
    {
        var open = OpenInner(_image);
        try
        {
            var f = open.Inner.GetAllFiles().FirstOrDefault(x => string.Equals(x.FullName, inImage, StringComparison.Ordinal))
                    ?? throw new FileNotFoundException("not in the image: " + inImage);
            return new OwnedStream(new StreamWrapper(f.GetView(), f.size), open);
        }
        catch
        {
            open.Dispose();
            throw;
        }
    }

    sealed class Opened : IDisposable
    {
        public FileStream File = null!;
        public PfsReader Inner = null!;
        public void Dispose() => File.Dispose();
    }

    // .ffpfs: the game's PFS; .ffpfsc: a PFS whose single file is the game's PFS, PFSC-compressed
    static Opened OpenInner(string image)
    {
        var fs = new FileStream(image, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 16);
        try
        {
            var outer = new PfsReader(new LibProsperoPkg.Util.StreamReader(fs, 0), 0, null, null, null, 0, false);
            var files = outer.GetAllFiles().ToList();
            PfsReader inner = outer;
            if (files.Count == 1 && files[0].FullName.EndsWith(".ffpfs", StringComparison.OrdinalIgnoreCase))
            {
                IMemoryReader view = files[0].GetView();
                if (files[0].flags.HasFlag(InodeFlags.compressed)) view = new PFSCReader(view);
                inner = new PfsReader(view);
            }
            return new Opened { File = fs, Inner = inner };
        }
        catch
        {
            fs.Dispose();
            throw;
        }
    }

    // a read-only stream that closes the image handle with it
    sealed class OwnedStream : Stream
    {
        readonly Stream _s; readonly IDisposable _owner;
        public OwnedStream(Stream s, IDisposable owner) { _s = s; _owner = owner; }
        public override bool CanRead => true;
        public override bool CanSeek => _s.CanSeek;
        public override bool CanWrite => false;
        public override long Length => _s.Length;
        public override long Position { get => _s.Position; set => _s.Position = value; }
        public override int Read(byte[] buffer, int offset, int count) => _s.Read(buffer, offset, count);
        public override long Seek(long offset, SeekOrigin origin) => _s.Seek(offset, origin);
        public override void Flush() { }
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing)
        {
            if (disposing) { _s.Dispose(); _owner.Dispose(); }
            base.Dispose(disposing);
        }
    }

    public void Dispose()
    {
        SourceHooks.Open = null;
        SourceHooks.Length = null;
        _standIns.Clear();
        _sizes.Clear();
    }
}
