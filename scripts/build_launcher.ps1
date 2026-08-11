[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $projectRoot "launcher\LocalAIWorkbenchLauncher.cs"
$pngPath = Join-Path $projectRoot "frontend\app-icon.png"
$iconPath = Join-Path $projectRoot "launcher\LocalAIWorkbench.ico"
$outputPath = Join-Path $projectRoot "LocalAIWorkbench.exe"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$iconBuilder = Join-Path $PSScriptRoot "build_windows_icon.py"
$compilerCandidates = @(
    (Join-Path $env:SystemRoot "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:SystemRoot "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$compilerPath = $compilerCandidates |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python virtual environment was not found at $pythonPath"
}
if (-not $compilerPath) {
    throw "The Windows .NET Framework C# compiler was not found."
}

& $pythonPath $iconBuilder --source $pngPath --output $iconPath
if ($LASTEXITCODE -ne 0) {
    throw "Unable to create the multi-size Windows icon."
}

& $compilerPath `
    /nologo `
    /target:winexe `
    /platform:anycpu `
    /optimize+ `
    /codepage:65001 `
    /reference:System.dll `
    /reference:System.Windows.Forms.dll `
    "/win32icon:$iconPath" `
    "/out:$outputPath" `
    $sourcePath
if ($LASTEXITCODE -ne 0) {
    throw "Unable to compile LocalAIWorkbench.exe."
}

$binary = [System.IO.File]::ReadAllBytes($outputPath)
if ($binary.Length -lt 2 -or $binary[0] -ne 0x4D -or $binary[1] -ne 0x5A) {
    throw "The launcher output is not a valid Windows PE file."
}

Write-Output "Built $outputPath"
