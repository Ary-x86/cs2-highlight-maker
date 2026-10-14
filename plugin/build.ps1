# Build the CS:DM CS2 server plugin (server.dll) for Windows with frame-hook.patch applied.
# Needs Git and Visual Studio 2022 or newer with the "Desktop development with C++" workload.
#   powershell -ExecutionPolicy Bypass -File plugin\build.ps1 [-Out dist\plugin] [-Toolset v143]
param(
    [string]$Out = (Join-Path $PSScriptRoot "..\dist\plugin"),
    [string]$Toolset = "v143"
)
$ErrorActionPreference = "Stop"
$Commit = "b45a5f29283590c3fd0fc6526564b49567c0b931"  # keep in sync with build.sh
$Src = Join-Path $env:TEMP "cs2hl-csdm-src"
$Patch = Join-Path $PSScriptRoot "frame-hook.patch"

# Not called "Git": PowerShell names are case-insensitive, so it would call itself.
function Invoke-Git { & git.exe -c core.autocrlf=false @args; if ($LASTEXITCODE) { throw "git $args failed" } }

if (-not (Test-Path "$Src\.git")) {
    Invoke-Git clone --filter=blob:none --sparse https://github.com/akiver/cs-demo-manager.git $Src
    Invoke-Git -C $Src sparse-checkout set cs2-server-plugin
}
Invoke-Git -C $Src fetch --depth 1 origin $Commit
Invoke-Git -C $Src checkout -q --force $Commit
foreach ($dep in "easywsclient", "hl2sdk", "json") {
    Invoke-Git -C $Src submodule update --init --depth 1 "cs2-server-plugin/cs2-server-plugin/deps/$dep"
}
Invoke-Git -C $Src apply --whitespace=nowarn $Patch

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
if (-not $msbuild) { throw "MSBuild not found. Install Visual Studio with the C++ workload." }

New-Item -ItemType Directory -Force $Out | Out-Null
$OutFull = (Resolve-Path $Out).Path.TrimEnd("\") + "\"
& $msbuild "$Src\cs2-server-plugin\cs2-server-plugin\cs2-server-plugin.vcxproj" `
    /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=$Toolset "/p:OutDir=$OutFull" /m /nologo /v:minimal
if ($LASTEXITCODE) { throw "MSBuild failed" }
Write-Host "built $OutFull`server.dll"
