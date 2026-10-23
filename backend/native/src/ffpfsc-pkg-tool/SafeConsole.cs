using System;
using System.IO;
using System.Text;

namespace PkgTool;

// File names inside a package are printed in progress and error lines. A name with a line
// break would start a line of its own that the app reads as a marker, so those lines keep
// their control characters escaped.
internal sealed class SafeLineWriter : TextWriter
{
    readonly TextWriter inner;

    SafeLineWriter(TextWriter inner) { this.inner = inner; }

    // Only the diagnostics on stderr: stdout also carries the JSON documents, whose own line
    // breaks must stay. The stdout lines that name files escape them where they are written.
    public static void Install() => Console.SetError(TextWriter.Synchronized(new SafeLineWriter(Console.Error)));

    public override Encoding Encoding => inner.Encoding;

    internal static string Escape(string? s)
    {
        if (string.IsNullOrEmpty(s)) return s ?? "";
        StringBuilder? sb = null;
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c >= 0x20 || c == '\t') { sb?.Append(c); continue; }
            sb ??= new StringBuilder(s, 0, i, s.Length + 8);
            sb.Append(c == '\n' ? "\\n" : c == '\r' ? "\\r" : "?");
        }
        return sb?.ToString() ?? s;
    }

    // Single characters and a write's own leading \r or trailing line end pass: that is how a
    // progress line is drawn. A name is written as part of a string, never char by char.
    public override void Write(char value) => inner.Write(value < 0x20 && value != '\t' && value != '\n' && value != '\r' ? '?' : value);
    public override void Write(string? value) => inner.Write(EscapeInner(value));
    public override void Write(char[] buffer, int index, int count) => inner.Write(EscapeInner(new string(buffer, index, count)));

    static string EscapeInner(string? s)
    {
        if (string.IsNullOrEmpty(s)) return s ?? "";
        int head = 0, tail = s.Length;
        while (head < tail && s[head] == '\r') head++;
        if (tail > head && s[tail - 1] == '\n') { tail--; if (tail > head && s[tail - 1] == '\r') tail--; }
        return s[..head] + Escape(s[head..tail]) + s[tail..];
    }
    public override void WriteLine() => inner.WriteLine();
    public override void WriteLine(string? value) => inner.WriteLine(Escape(value));
    public override void Flush() => inner.Flush();
}
