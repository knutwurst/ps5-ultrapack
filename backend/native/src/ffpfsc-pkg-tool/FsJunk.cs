// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
// Additional terms under section 7 of the GPL apply, see the NOTICE file.
using System;
using System.Collections.Generic;

namespace PkgTool;

// OS and archiver clutter, never packed. keep in sync with the Python lists, a test checks
internal static class FsJunk
{
    static readonly HashSet<string> Names = new(StringComparer.Ordinal)
    {
        ".ds_store", ".localized", ".lsoverride", ".apdisk", ".volumeicon.icns", "icon\r",
        "thumbs.db", "ehthumbs.db", "desktop.ini",
        "__macosx", ".spotlight-v100", ".trashes", ".fseventsd", ".temporaryitems",
        ".documentrevisions-v100", ".appledouble", "$recycle.bin", "system volume information",
    };

    public static bool IsJunkName(string name) =>
        name.StartsWith("._", StringComparison.Ordinal) || Names.Contains(name.ToLowerInvariant());

    public static bool PathHasJunk(string relativePath)
    {
        foreach (var part in relativePath.Split('/', '\\'))
            if (part.Length > 0 && IsJunkName(part)) return true;
        return false;
    }
}
