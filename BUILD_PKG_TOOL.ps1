#!/usr/bin/env pwsh
# Build the fPKG helper for Windows x64 from backend\native\src\ffpfsc-pkg-tool into
# backend\native\: ffpfsc-pkg-tool.exe and the ImageMagick library it loads from its own
# folder. Neither is tracked in git. Needs the .NET SDK 9 or newer.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Dotnet = $env:DOTNET
if (-not $Dotnet) {
    $Cmd = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($Cmd) { $Dotnet = $Cmd.Source }
    elseif (Test-Path "$env:USERPROFILE\.dotnet\dotnet.exe") { $Dotnet = "$env:USERPROFILE\.dotnet\dotnet.exe" }
}
if (-not $Dotnet -or -not (Test-Path $Dotnet)) {
    Write-Error "BUILD_PKG_TOOL.ps1: the .NET SDK (9 or newer) is required: https://dotnet.microsoft.com/download (or set `$env:DOTNET to dotnet.exe)"
}

$Src = "backend\native\src\ffpfsc-pkg-tool"
$Out = "$Src\out"
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
& $Dotnet publish "$Src\PkgTool.csproj" -c Release -r win-x64 --self-contained true -o $Out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

foreach ($f in "ffpfsc-pkg-tool.exe", "Magick.Native-Q8-x64.dll") {
    Copy-Item -Force "$Out\$f" "backend\native\$f"
}
& "backend\native\ffpfsc-pkg-tool.exe" version | Select-Object -First 1
