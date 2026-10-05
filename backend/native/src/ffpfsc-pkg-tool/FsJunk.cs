using System;
using System.Collections.Generic;

namespace PkgTool;

/// <summary>
/// OS and archiver clutter that never enters a package: not staged, not listed for the inner
/// image, not indexed. The same names as the app's Python side (backend/cli.py, ultra_core.py,
/// backend/after_job.py, backend/mkpfs/utils.py); a test there keeps the lists equal.
/// </summary>
internal static class FsJunk
{
    static readonly HashSet<string> Names = new(StringComparer.Ordinal)
    {
        ".ds_store", ".localized", ".lsoverride", ".apdisk", ".volumeicon.icns", "icon\r",
        "thumbs.db", "ehthumbs.db", "desktop.ini",
        "__macosx", ".spotlight-v100", ".trashes", ".fseventsd", ".temporaryitems",
        ".documentrevisions-v100", ".appledouble", "$recycle.bin", "system volume information",
    };

    /// <summary>True for an AppleDouble sidecar (._*) or a known clutter name, case-insensitive.</summary>
    public static bool IsJunkName(string name) =>
        name.StartsWith("._", StringComparison.Ordinal) || Names.Contains(name.ToLowerInvariant());

    /// <summary>True when any segment of <paramref name="relativePath"/> is clutter.</summary>
    public static bool PathHasJunk(string relativePath)
    {
        foreach (var part in relativePath.Split('/', '\\'))
            if (part.Length > 0 && IsJunkName(part)) return true;
        return false;
    }
}
