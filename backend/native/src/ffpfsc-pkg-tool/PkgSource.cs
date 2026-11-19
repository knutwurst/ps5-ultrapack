// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
// Additional terms under section 7 of the GPL apply, see the NOTICE file.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using LibProsperoPkg.PFS;
using LibProsperoPkg.PFS.Compression;
using LibProsperoPkg.Util;

namespace PkgTool;

// a PS5 .pkg as a build source without unpacking it: the tree extract-inner would write, with the
// big files as empty stand-ins read from the package, and its Kraken blocks handed back to the encoder
internal sealed class PkgSource : IDisposable
{
    const long StandInMin = 1 << 20;
    const int Half = 131072;
    const long ReuseCapBytes = 512L << 20;

    readonly InnerImage _img;
    readonly IReadOnlyList<ProsperoNapsSpan> _spans;
    readonly ConcurrentDictionary<string, (long offset, long size)> _standIns = new(StringComparer.Ordinal);
    readonly bool _reuse;
    readonly bool _reuseStored;

    // blocks seen while reading, keyed by what they decode to; oldest go first past the cap
    readonly Dictionary<BlockKey, Entry> _blocks = new();
    readonly Queue<(BlockKey key, long seq)> _order = new();
    readonly object _gate = new();
    long _seq, _heldBytes;
    long _reused, _keptStored, _encoded, _skipped;

    public string Root { get; }
    public int Written { get; private set; }
    public int StandIns => _standIns.Count;
    public long StandInBytes { get; private set; }
    public long Listed { get; private set; }     // every file the reader sees, junk included
    public long Reused => Interlocked.Read(ref _reused);
    public long KeptStored => Interlocked.Read(ref _keptStored);
    public long Encoded => Interlocked.Read(ref _encoded);
    public long Unusable => Interlocked.Read(ref _skipped);

    public static bool IsPackage(string path)
        => File.Exists(path) && path.EndsWith(".pkg", StringComparison.OrdinalIgnoreCase);

    public PkgSource(string pkg, string passcode, string stubRoot, bool reuse,
                     Func<string, string, string, List<string>, Dictionary<string, string>> liftCnt)
    {
        if (!NapsBlockReader.UsesSpanDecoder) throw new NotSupportedException("the library's NAPS span decoder is not available");
        Root = Path.GetFullPath(stubRoot);
        _img = new InnerImage(pkg, passcode, cacheBlockSize: 4 << 20, cacheBlocks: 8);
        try
        {
            _spans = _img.Plan.Spans;
            // a package without one Kraken block was built uncompressed: its raw blocks are no verdict
            _reuseStored = _spans.Any(s => s.CompressedLength != s.UncompressedLength);
            _reuse = reuse;
            Directory.CreateDirectory(Root);
            foreach (var d in _img.AllDirs())
            {
                var rel = Safe(_img.RelativePath(d));
                if (!FsJunk.PathHasJunk(rel)) Directory.CreateDirectory(Target(rel));
            }
            var all = _img.Pfs.GetAllFiles().OrderBy(f => _img.RelativePath(f), StringComparer.Ordinal).ToList();
            Listed = all.Count;
            foreach (var f in all)
            {
                var rel = Safe(_img.RelativePath(f));
                if (FsJunk.PathHasJunk(rel)) continue;
                var dst = Target(rel);
                Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                if (WriteOut(rel, f))
                {
                    using var o = new FileStream(dst, FileMode.CreateNew, FileAccess.Write);
                    _img.CopyFile(f, o);
                    Written++;
                }
                else
                {
                    using (new FileStream(dst, FileMode.CreateNew, FileAccess.Write)) { }
                    _standIns[dst] = (f.offset, f.size);
                    StandInBytes += f.size;
                }
            }
            // param.json, icons and PlayGo live in the CNT: they win over same-named PFS files, as in extract-inner
            var errors = new List<string>();
            var tmpCnt = Path.Combine(Root, ".cnt-" + Guid.NewGuid().ToString("N"));
            try
            {
                foreach (var kv in liftCnt(pkg, passcode, tmpCnt, errors))
                {
                    var dst = Target(Safe(kv.Key));
                    Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                    if (_standIns.TryRemove(dst, out var was)) StandInBytes -= was.size;
                    File.Copy(kv.Value, dst, overwrite: true);
                }
            }
            finally
            {
                try { Directory.Delete(tmpCnt, true); } catch { }
            }
            if (errors.Count > 0)
                throw new InvalidDataException("the sce_sys metadata in the CNT could not be read: " + string.Join("; ", errors));
        }
        catch
        {
            _img.Dispose();
            throw;
        }
        SourceHooks.Length = path => StandIn(path, out var s) ? s.size : -1;
        SourceHooks.Open = path => StandIn(path, out var s) ? new SpanStream(this, s.offset, s.size) : null;
        if (_reuse) SourceHooks.ReuseBlock = Reuse;
    }

    string Target(string rel)
    {
        var full = Path.GetFullPath(Path.Combine(Root, rel.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(Root + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            throw new InvalidDataException("the package names a path that leaves the game folder: " + rel);
        return full;
    }

    static string Safe(string rel)
    {
        var r = rel.Replace('\\', '/').Trim('/');
        if (r.Length == 0 || r.Split('/').Any(p => p is "" or "." or ".."))
            throw new InvalidDataException("unsafe package path: " + rel);
        return r;
    }

    // only while the stub is still the empty file we made: a file the build rewrote is read as it is
    bool StandIn(string? path, out (long offset, long size) s)
    {
        s = default;
        if (path == null) return false;
        var full = Path.GetFullPath(path);
        if (!_standIns.TryGetValue(full, out s)) return false;
        try { return new FileInfo(full).Length == 0; } catch { return false; }
    }

    // the same split as SourceImage, plus anything the library would not read as one plain extent
    bool WriteOut(string rel, PfsReader.File f)
    {
        if (f.size < StandInMin || f.flags.HasFlag(InodeFlags.compressed) || f.blocks != null) return true;
        var top = rel.Split('/')[0];
        if (top.Equals("sce_sys", StringComparison.OrdinalIgnoreCase)
            || top.Equals("sce_module", StringComparison.OrdinalIgnoreCase)
            || top.StartsWith("fakelib", StringComparison.OrdinalIgnoreCase)) return true;
        var name = Path.GetFileName(rel);
        if (name.Equals("eboot.bin", StringComparison.Ordinal)) return true;
        var ext = Path.GetExtension(name).ToLowerInvariant();
        if (ext is ".prx" or ".sprx" or ".elf" or ".self") return true;
        var head = new byte[4];
        f.GetView().Read(0, head, 0, head.Length);
        return (head[0] == 0x7F && head[1] == (byte)'E' && head[2] == (byte)'L' && head[3] == (byte)'F')
               || (head[0] == 0x4F && head[1] == 0x15 && head[2] == 0x3D && head[3] == 0x1D)
               || (head[0] == 0x54 && head[1] == 0x14 && head[2] == 0xF5 && head[3] == 0xEE);
    }

    int SpanAt(long logical)
    {
        int lo = 0, hi = _spans.Count - 1, hit = -1;
        while (lo <= hi)
        {
            int mid = (lo + hi) >> 1;
            var s = _spans[mid];
            if (s.UncompressedOffset + s.UncompressedLength > logical) { hit = mid; hi = mid - 1; }
            else lo = mid + 1;
        }
        if (hit < 0 || _spans[hit].UncompressedOffset > logical)
            throw new InvalidDataException($"no NAPS span holds logical offset 0x{logical:x}.");
        return hit;
    }

    byte[] Decode(int index)
    {
        var span = _spans[index];
        var stored = _img.ReadStored(span);
        var data = NapsBlockReader.DecodeStored(_img.Layout, span, stored);
        if (data.Length != span.UncompressedLength) throw new InvalidDataException($"NAPS span {span.Index} decoded to the wrong size.");
        if (_reuse) Remember(span, stored, data);
        return data;
    }

    readonly record struct BlockKey(ulong A, ulong B, ulong C, ulong D, int Length);

    sealed record Entry(byte[]? Payload, bool Multi, int First, int Flags, long Seq = 0);

    static BlockKey KeyOf(ReadOnlySpan<byte> data)
    {
        Span<byte> h = stackalloc byte[32];
        SHA256.HashData(data, h);
        return new BlockKey(BitConverter.ToUInt64(h[..8]), BitConverter.ToUInt64(h[8..16]),
                            BitConverter.ToUInt64(h[16..24]), BitConverter.ToUInt64(h[24..]), data.Length);
    }

    // keeps a span's stored block under the digest of the bytes it decodes to
    void Remember(ProsperoNapsSpan span, byte[] stored, byte[] data)
    {
        // a shuffled span decodes to other bytes than the encoder saw: never reused
        if (span.ShuffleIndex != 0 || span.KdePredictor != 0) { Interlocked.Increment(ref _skipped); return; }
        Entry e;
        if (stored.Length == data.Length)
        {
            if (!_reuseStored) return;
            e = new Entry(null, false, 0, 0);
        }
        else
        {
            int u = data.Length;
            bool multi = u > Half;
            int first = multi ? span.FirstChunkCompressedLength : stored.Length;
            if (first <= 0 || first > stored.Length || (multi && first == stored.Length)) { Interlocked.Increment(ref _skipped); return; }
            // the form nibble per half, as EncodedBlock builds it: verbatim 12, bare entropy 4, LZ 6/2 plus 1 for sub literals
            int n0 = first == Math.Min(u, Half) ? 12 : (span.Even & 4) != 0 ? 6 | ((span.Even & 2) != 0 ? 1 : 0) : 4;
            int flags = n0;
            if (multi)
            {
                int n1 = stored.Length - first == u - Half ? 12 : (span.Odd & 4) != 0 ? 2 | ((span.Odd & 2) != 0 ? 1 : 0) : 4;
                flags |= n1 << 4;
            }
            // the NAPS modes the library writes for these flags must be the ones the package holds
            byte even = (byte)(1 | ((flags & 2) != 0 ? 4 : 0) | ((flags & 1) != 0 ? 2 : 0));
            byte odd = multi ? (byte)(((flags & 0x20) == 0 ? 1 : 4) | ((flags & 0x10) != 0 ? 2 : 0)) : (byte)0;
            if (even != span.Even || odd != span.Odd) { Interlocked.Increment(ref _skipped); return; }
            e = new Entry(stored, multi, first, flags);
        }
        var key = KeyOf(data);
        lock (_gate)
        {
            if (_blocks.TryGetValue(key, out var prev)) _heldBytes -= Cost(prev);
            e = e with { Seq = ++_seq };
            _blocks[key] = e;
            _heldBytes += Cost(e);
            _order.Enqueue((key, e.Seq));
            while (_heldBytes > ReuseCapBytes && _order.TryDequeue(out var old))
                if (_blocks.TryGetValue(old.key, out var cur) && cur.Seq == old.seq)
                {
                    _blocks.Remove(old.key);
                    _heldBytes -= Cost(cur);
                }
        }
    }

    static long Cost(Entry e) => (e.Payload?.Length ?? 0) + 64;

    // SourceHooks.ReuseBlock: the old block for exactly these bytes, empty for a stored one, null to encode
    byte[]? Reuse(ReadOnlySpan<byte> input, int level, out bool multiChunk, out int firstChunkSize, out int boundaryFlags)
    {
        multiChunk = false; firstChunkSize = 0; boundaryFlags = 0;
        var key = KeyOf(input);
        Entry? e;
        lock (_gate) _blocks.TryGetValue(key, out e);
        if (e == null) { Interlocked.Increment(ref _encoded); return null; }
        if (e.Payload == null) { Interlocked.Increment(ref _keptStored); return Array.Empty<byte>(); }
        Interlocked.Increment(ref _reused);
        multiChunk = e.Multi; firstChunkSize = e.First; boundaryFlags = e.Flags;
        return e.Payload.ToArray();
    }

    // a stand-in's bytes, decoded a batch of spans at a time on all cores
    sealed class SpanStream : Stream
    {
        readonly PkgSource _src;
        readonly long _base, _len;
        long _pos;
        readonly Dictionary<int, byte[]> _window = new();

        public SpanStream(PkgSource src, long offset, long size) { _src = src; _base = offset; _len = size; }
        public override bool CanRead => true;
        public override bool CanSeek => true;
        public override bool CanWrite => false;
        public override long Length => _len;
        public override long Position { get => _pos; set => _pos = value; }

        byte[] Span(int index)
        {
            if (_window.TryGetValue(index, out var d)) return d;
            _window.Clear();
            int last = _src.SpanAt(_base + _len - 1);
            int count = Math.Min(last - index + 1, Math.Max(2, Environment.ProcessorCount * 2));
            var got = new byte[count][];
            Parallel.For(0, count, i => got[i] = _src.Decode(index + i));
            for (int i = 0; i < count; i++) _window[index + i] = got[i];
            return got[0];
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            int done = 0;
            while (count > 0 && _pos < _len)
            {
                long logical = _base + _pos;
                int index = _src.SpanAt(logical);
                var data = Span(index);
                var span = _src._spans[index];
                int at = (int)(logical - span.UncompressedOffset);
                int n = (int)Math.Min(Math.Min(count, data.Length - at), _len - _pos);
                Buffer.BlockCopy(data, at, buffer, offset, n);
                _pos += n; offset += n; count -= n; done += n;
            }
            return done;
        }

        public override long Seek(long offset, SeekOrigin origin)
            => _pos = origin switch { SeekOrigin.Begin => offset, SeekOrigin.Current => _pos + offset, _ => _len + offset };
        public override void Flush() { }
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing) { _window.Clear(); base.Dispose(disposing); }
    }

    public void Dispose()
    {
        SourceHooks.ReuseBlock = null;
        SourceHooks.Open = null;
        SourceHooks.Length = null;
        _standIns.Clear();
        lock (_gate) _blocks.Clear();
        _img.Dispose();
    }
}
