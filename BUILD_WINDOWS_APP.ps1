#!/usr/bin/env pwsh
# Build "dist\windows\PS5 UltraPack\PS5 UltraPack.exe" (Windows x64) plus the release zip and
# its SHA-256. Run from the repository root with an x64 Python that has requirements-build.txt
# installed. The fPKG helper and the UnRAR module must be in place: BUILD_WINDOWS_PARTS.sh
# builds both on a Mac, BUILD_PKG_TOOL.ps1 and "pip install .\backend\unrar" on Windows.
param([string]$Python = "python", [string]$WorkPath = "build\windows")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Version = (Select-String -Path PS5_UltraPack.py -Pattern '^APP_VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
& $Python -m PyInstaller --clean --noconfirm --distpath dist\windows --workpath $WorkPath PS5_UltraPack_windows.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Windows builds are a preview for now, and the zip name says so.
$Zip = "dist\PS5-UltraPack-$Version-windows-x64-preview.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $Zip, "$Zip.sha256"
Compress-Archive -Path "dist\windows\PS5 UltraPack" -DestinationPath $Zip
$Hash = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
"$Hash  $(Split-Path -Leaf $Zip)" | Set-Content -Encoding ascii "$Zip.sha256"
"Built: dist\windows\PS5 UltraPack\PS5 UltraPack.exe"
"Release: $Zip"
$Hash
