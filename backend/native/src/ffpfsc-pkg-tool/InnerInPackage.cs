// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
// Additional terms under section 7 of the GPL apply, see the NOTICE file.
using System;
using System.Collections.Generic;
using System.IO;
using LibProsperoPkg.Util;

namespace PkgTool;

// The library builds the package's inner image (pfs_image.dat) as a file in temp and then copies it
// into the package, where it starts at 0x10000. This writes it there from the start: the library's
// spool lives in a file of ours in the output folder at that offset, and when the library creates
// the package that file becomes the package. The outer PFS then writes each block back where it
// came from. If the library re-orders blocks, a range it overwrites before reading is kept aside.
internal sealed class InnerInPackage : IDisposable
{
    public const long InnerOffset = 65536;

    readonly string _outDir, _pkgName, _sideDir;
    readonly string _holder;
    readonly Func<string, long>? _prevLength;
    readonly object _gate = new();
    string? _spool;
    string? _package;
    long _spoolLength = -1;

    // after the package took over: what the outer PFS has read, and what was kept aside
    readonly List<(long s, long e)> _read = new();
    readonly List<(long s, long e, long side)> _kept = new();
    FileStream? _side;
    public long KeptBytes { get; private set; }

    public bool Adopted => _package != null;

    public InnerInPackage(string outDir, string pkgFileName, string sideDir)
    {
        _outDir = Path.GetFullPath(outDir);
        _pkgName = pkgFileName;
        _sideDir = sideDir;
        _holder = Path.Combine(_outDir, ".inner-" + Guid.NewGuid().ToString("N").Substring(0, 12) + ".tmp");
        _prevLength = SourceHooks.Length;
        SourceHooks.OpenFile = Open;
        SourceHooks.Length = path =>
        {
            if (path != null && _spool != null && SamePath(path, _spool)) return SpoolLength();
            return _prevLength?.Invoke(path!) ?? -1;
        };
    }

    static bool SamePath(string a, string b)
        => string.Equals(Path.GetFullPath(a), b, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);

    static bool IsSpool(string full)
    {
        var n = Path.GetFileName(full);
        return n.StartsWith("libprospero-publisher-", StringComparison.Ordinal) && n.EndsWith(".pfs_image.dat", StringComparison.Ordinal);
    }

    bool IsPackage(string full)
    {
        var n = Path.GetFileName(full);
        return string.Equals(Path.GetDirectoryName(full), _outDir, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal)
               && n.StartsWith("." + _pkgName + ".", StringComparison.Ordinal) && n.EndsWith(".build.tmp", StringComparison.Ordinal);
    }

    long SpoolLength()
    {
        lock (_gate)
        {
            if (_spoolLength >= 0) return _spoolLength;
            return File.Exists(_holder) ? Math.Max(0, new FileInfo(_holder).Length - InnerOffset) : 0;
        }
    }

    FileStream? Open(string path, int mode)
    {
        if (path == null) return null;
        var full = Path.GetFullPath(path);
        lock (_gate)
        {
            if (_spool == null && IsSpool(full)) _spool = full;
            if (_spool != null && SamePath(full, _spool))
            {
                if (_package == null)
                {
                    bool fresh = mode is (int)FileMode.Create or (int)FileMode.CreateNew or (int)FileMode.Truncate;
                    var fs = new SpoolStream(_holder, null, fresh);
                    return fs;
                }
                return new SpoolStream(_package, this, false);
            }
            if (_package == null && _spool != null && IsPackage(full)
                && mode is (int)FileMode.Create or (int)FileMode.CreateNew && File.Exists(_holder))
            {
                _spoolLength = Math.Max(0, new FileInfo(_holder).Length - InnerOffset);
                File.Move(_holder, full);
                _package = full;
                _read.Clear();
                Console.Error.WriteLine($"  [inner] the inner image ({_spoolLength / 1073741824.0:F2} GB) is already in place in the package; no copy");
                return new PackageWriter(full, this);
            }
            if (_spool != null && full.EndsWith(".layout.tmp", StringComparison.Ordinal)
                && Path.GetFileName(full).StartsWith(Path.GetFileName(_spool), StringComparison.Ordinal))
                throw new InvalidOperationException("the library wants to rewrite the inner image in a second file; build with --no-inner-in-package");
        }
        return null;
    }

    // a read of the inner image once it is in the package: kept-aside ranges first, then the file
    int ReadInner(FileStream file, long pos, Span<byte> dst)
    {
        lock (_gate)
        {
            long len = _spoolLength;
            if (pos >= len) return 0;
            int n = (int)Math.Min(dst.Length, len - pos);
            int done = 0;
            while (done < n)
            {
                long p = pos + done;
                var k = _kept.Find(x => x.s <= p && p < x.e);
                int chunk;
                if (k != default)
                {
                    chunk = (int)Math.Min(n - done, k.e - p);
                    _side!.Position = k.side + (p - k.s);
                    _side.ReadExactly(dst.Slice(done, chunk));
                }
                else
                {
                    long next = long.MaxValue;
                    foreach (var x in _kept) if (x.s > p && x.s < next) next = x.s;
                    chunk = (int)Math.Min(n - done, next - p);
                    RandomAccess.Read(file.SafeFileHandle, dst.Slice(done, chunk), InnerOffset + p);
                }
                done += chunk;
            }
            AddRange(_read, pos, pos + n);
            return n;
        }
    }

    // before the package writer overwrites [start, end) of the file: keep what the inner image still needs
    void Preserve(FileStream file, long start, long end)
    {
        lock (_gate)
        {
            long s = Math.Max(start - InnerOffset, 0), e = Math.Min(end - InnerOffset, _spoolLength);
            if (e <= s) return;
            foreach (var (us, ue) in Missing(s, e))
            {
                _side ??= new FileStream(Path.Combine(_sideDir, "inner-kept-" + Guid.NewGuid().ToString("N").Substring(0, 12) + ".tmp"),
                                         FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None, 1 << 16, FileOptions.DeleteOnClose);
                var buf = new byte[ue - us];
                RandomAccess.Read(file.SafeFileHandle, buf, InnerOffset + us);
                long at = _side.Length;
                _side.Position = at;
                _side.Write(buf);
                _kept.Add((us, ue, at));
                KeptBytes += ue - us;
            }
        }
    }

    // parts of [s, e) neither read yet nor kept already
    List<(long, long)> Missing(long s, long e)
    {
        var covered = new List<(long s, long e)>(_read);
        foreach (var k in _kept) covered.Add((k.s, k.e));
        covered.Sort((a, b) => a.s.CompareTo(b.s));
        var gaps = new List<(long, long)>();
        long cur = s;
        foreach (var (cs, ce) in covered)
        {
            if (ce <= cur) continue;
            if (cs >= e) break;
            if (cs > cur) gaps.Add((cur, Math.Min(cs, e)));
            cur = Math.Max(cur, ce);
            if (cur >= e) break;
        }
        if (cur < e) gaps.Add((cur, e));
        return gaps;
    }

    static void AddRange(List<(long s, long e)> set, long s, long e)
    {
        if (e <= s) return;
        set.Add((s, e));
        set.Sort((a, b) => a.s.CompareTo(b.s));
        var merged = new List<(long s, long e)>();
        foreach (var r in set)
        {
            if (merged.Count > 0 && r.s <= merged[^1].e) merged[^1] = (merged[^1].s, Math.Max(merged[^1].e, r.e));
            else merged.Add(r);
        }
        set.Clear();
        set.AddRange(merged);
    }

    public void Dispose()
    {
        SourceHooks.OpenFile = null;
        SourceHooks.Length = _prevLength;
        lock (_gate)
        {
            _side?.Dispose();
            if (_package == null) { try { File.Delete(_holder); } catch { } }
        }
    }

    // the inner image as the library sees it: offset 0 is 0x10000 into our file
    sealed class SpoolStream : FileStream
    {
        readonly InnerInPackage? _owner;   // set once the image lives in the package: reads only
        long _pos;

        public SpoolStream(string path, InnerInPackage? owner, bool fresh)
            : base(path, owner == null ? FileMode.OpenOrCreate : FileMode.Open,
                   owner == null ? FileAccess.ReadWrite : FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 0)
        {
            _owner = owner;
            if (owner == null && (fresh || base.Length < InnerOffset)) base.SetLength(InnerOffset);
        }

        public override bool CanSeek => true;
        public override bool CanWrite => _owner == null;
        public override long Length => _owner != null ? _owner._spoolLength : Math.Max(0, base.Length - InnerOffset);
        public override long Position { get => _pos; set => _pos = value; }
        public override long Seek(long offset, SeekOrigin origin)
            => _pos = origin switch { SeekOrigin.Begin => offset, SeekOrigin.Current => _pos + offset, _ => Length + offset };
        public override void SetLength(long value)
        {
            if (_owner != null) throw new NotSupportedException();
            base.SetLength(value + InnerOffset);
        }
        public override int Read(byte[] buffer, int offset, int count) => Read(buffer.AsSpan(offset, count));
        public override int Read(Span<byte> buffer)
        {
            int n = _owner != null ? _owner.ReadInner(this, _pos, buffer)
                                   : RandomAccess.Read(SafeFileHandle, buffer.Slice(0, (int)Math.Max(0, Math.Min(buffer.Length, Length - _pos))), InnerOffset + _pos);
            _pos += n;
            return n;
        }
        public override int ReadByte() { Span<byte> b = stackalloc byte[1]; return Read(b) == 1 ? b[0] : -1; }
        public override void Write(byte[] buffer, int offset, int count) => Write(buffer.AsSpan(offset, count));
        public override void Write(ReadOnlySpan<byte> buffer)
        {
            if (_owner != null) throw new NotSupportedException();
            RandomAccess.Write(SafeFileHandle, buffer, InnerOffset + _pos);
            _pos += buffer.Length;
        }
        public override void WriteByte(byte value) => Write(new[] { value }, 0, 1);
        public override void Flush() { }
        public override void Flush(bool flushToDisk) { }
    }

    // the package itself while the outer PFS is written into it
    sealed class PackageWriter : FileStream
    {
        readonly InnerInPackage _owner;
        long _pos;

        public PackageWriter(string path, InnerInPackage owner)
            : base(path, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite | FileShare.Delete, 0)
        { _owner = owner; }

        public override bool CanSeek => true;
        public override long Position { get => _pos; set => _pos = value; }
        public override long Seek(long offset, SeekOrigin origin)
            => _pos = origin switch { SeekOrigin.Begin => offset, SeekOrigin.Current => _pos + offset, _ => Length + offset };
        public override void SetLength(long value)
        {
            if (value < base.Length) _owner.Preserve(this, value, base.Length);
            base.SetLength(value);
        }
        public override int Read(byte[] buffer, int offset, int count) => Read(buffer.AsSpan(offset, count));
        public override int Read(Span<byte> buffer)
        {
            int n = RandomAccess.Read(SafeFileHandle, buffer, _pos);
            _pos += n;
            return n;
        }
        public override void Write(byte[] buffer, int offset, int count) => Write(buffer.AsSpan(offset, count));
        public override void Write(ReadOnlySpan<byte> buffer)
        {
            _owner.Preserve(this, _pos, _pos + buffer.Length);
            RandomAccess.Write(SafeFileHandle, buffer, _pos);
            _pos += buffer.Length;
        }
        public override void WriteByte(byte value) => Write(new[] { value }, 0, 1);
        public override void Flush() { }
        public override void Flush(bool flushToDisk) { }
    }
}
