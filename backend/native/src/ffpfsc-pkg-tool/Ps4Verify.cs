// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
// Additional terms under section 7 of the GPL apply, see the NOTICE file.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Orbis = LibOrbisPkg;

namespace PkgTool;

// a PS4 package's own checksums, recomputed with LibOrbisPkg's validator. The digest over the
// whole content image reads every byte, so it runs only with --full
internal static partial class Program
{
    internal sealed class Ps4CheckRow
    {
        public string name { get; set; } = "";
        public string kind { get; set; } = "";       // hash | signature | limit
        public string result { get; set; } = "";     // ok | fail | nokey | skipped | error
        public string detail { get; set; } = "";
    }

    internal sealed class Ps4VerifyDoc
    {
        public string content_id { get; set; } = "";
        public uint content_type { get; set; }
        public bool full { get; set; }
        public List<Ps4CheckRow> checks { get; set; } = new();
    }

    const string WholeImage = "PFS Image Digest";

    static int CmdPs4Verify(string[] args)
    {
        if (args.Length < 2) return Bad("ps4-verify needs <pkg> [--full]");
        var path = args[1];
        bool full = false;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--full") full = true;
            else return Bad("unknown ps4-verify flag: " + args[i]);
        }
        if (!File.Exists(path)) throw new FileNotFoundException("package not found", path);
        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20);
        var pkg = new Orbis.PKG.PkgReader(fs).ReadPkg();
        var doc = new Ps4VerifyDoc
        {
            content_id = pkg.Header.content_id ?? "",
            content_type = (uint)pkg.Header.content_type,
            full = full,
        };
        var validator = new Orbis.PKG.PkgValidator(pkg);
        foreach (var v in validator.Validations(fs))
        {
            var row = new Ps4CheckRow
            {
                name = v.Name,
                kind = v.Type.ToString().ToLowerInvariant(),
                detail = v.Description,
            };
            if (!full && v.Name == WholeImage)
            {
                row.result = "skipped";
            }
            else
            {
                try
                {
                    row.result = v.Validate() switch
                    {
                        Orbis.PKG.PkgValidator.ValidationResult.Ok => "ok",
                        Orbis.PKG.PkgValidator.ValidationResult.Fail => "fail",
                        _ => "nokey",
                    };
                }
                catch (Exception ex)
                {
                    row.result = "error";
                    row.detail = $"{ex.GetType().Name}: {ex.Message}";
                }
            }
            doc.checks.Add(row);
        }
        Console.WriteLine(JsonSerializer.Serialize(doc, PkgToolJsonContext.Default.Ps4VerifyDoc));
        return 0;
    }
}
