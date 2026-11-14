#!/usr/bin/env pwsh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2024-2026 Knutwurst, https://github.com/knutwurst/ps5-ultrapack
# Additional terms under section 7 of the GPL apply, see the NOTICE file.
# builds the Windows x64 app, release zip and SHA-256; run from the repo root
# pkg tool and UnRAR first: BUILD_WINDOWS_PARTS.sh (Mac) or BUILD_PKG_TOOL.ps1 + pip (Windows)
param([string]$Python = "python", [string]$WorkPath = "build\windows")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Version = (Select-String -Path PS5_UltraPack.py -Pattern '^APP_VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
& $Python -m PyInstaller --clean --noconfirm --distpath dist\windows --workpath $WorkPath PS5_UltraPack_windows.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Copy-Item -LiteralPath LICENSE, NOTICE -Destination "dist\windows\PS5 UltraPack"

$Zip = "dist\PS5-UltraPack-$Version-windows-x64.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $Zip, "$Zip.sha256"
Compress-Archive -Path "dist\windows\PS5 UltraPack" -DestinationPath $Zip
$Hash = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
# LF only, so shasum -c works on macOS and Linux too
[IO.File]::WriteAllText("$PWD\$Zip.sha256", "$Hash  $(Split-Path -Leaf $Zip)`n")
"Built: dist\windows\PS5 UltraPack\PS5 UltraPack.exe"
"Release: $Zip"
$Hash
