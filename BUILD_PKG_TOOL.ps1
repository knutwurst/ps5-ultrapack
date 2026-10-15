#!/usr/bin/env pwsh
# Build the fPKG helper from backend\native\src\ffpfsc-pkg-tool into backend\native\:
# the executable ffpfsc-pkg-tool.exe and the ImageMagick library it loads from its own folder.
# Neither is tracked in git. Needs the .NET SDK 9 or newer.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# Locate dotnet executable
$Dotnet = $env:DOTNET
if (-not $Dotnet) {
    $DotnetCmd = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($DotnetCmd) {
        $Dotnet = $DotnetCmd.Source
    } else {
        $Fallback = Join-Path $env:USERPROFILE ".dotnet\dotnet.exe"
        if (Test-Path $Fallback) {
            $Dotnet = $Fallback
        }
    }
}

if (-not $Dotnet -or -not (Test-Path $Dotnet)) {
    Write-Error "BUILD_PKG_TOOL.ps1: the .NET SDK (9 or newer) is required: https://dotnet.microsoft.com/download`n  (or point `$env:DOTNET at the dotnet.exe executable)"
    exit 1
}

# Detect Windows architecture (x64 or arm64)
$Arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
$Runtime = "win-$Arch"
$MagickDll = "Magick.Native-Q8-$Arch.dll"

$Src = "backend\native\src\ffpfsc-pkg-tool"
$Out = "$Src\out"

if (Test-Path $Out) {
    Remove-Item -Recurse -Force $Out
}

& $Dotnet publish "$Src\PkgTool.csproj" -c Release -r $Runtime --self-contained true -o $Out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Files = @("ffpfsc-pkg-tool.exe", $MagickDll)
foreach ($f in $Files) {
    $Dest = "backend\native\$f"
    if (Test-Path $Dest) {
        Remove-Item -Force $Dest
    }
    Copy-Item "$Out\$f" $Dest
}

& "backend\native\ffpfsc-pkg-tool.exe" version | Select-Object -First 1