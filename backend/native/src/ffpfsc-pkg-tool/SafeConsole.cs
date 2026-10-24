using System;
using System.IO;
using System.Text;

namespace PkgTool;

// a file name with a line break could fake a marker line, so escape control chars
internal sealed class SafeLineWriter : TextWriter
{
    readonly TextWriter inner;

    SafeLineWriter(TextWriter inner) { this.inner = inner; }

    // stderr only: stdout carries JSON whose line breaks must stay
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

    // single chars and a leading CR or trailing newline pass, that's how progress lines draw
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
