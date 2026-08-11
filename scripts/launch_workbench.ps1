[CmdletBinding()]
param(
    [switch]$SkipUpdate,
    [switch]$SmokeTest,
    [switch]$NoBrowser,
    [switch]$UpdateResume,
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 8080
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot "start_workbench.ps1"
$updateScript = Join-Path $PSScriptRoot "update_workbench.ps1"
$launcherPath = Join-Path $projectRoot "LocalAIWorkbench.exe"
$runtimeLogDir = Join-Path $projectRoot "runtime\logs"
$launchLog = Join-Path $runtimeLogDir "launcher-bootstrap.log"
$powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function Write-BootstrapLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force -Path $runtimeLogDir | Out-Null
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $launchLog -Value "[$timestamp] $Message" -Encoding UTF8
}

function Test-WorkbenchUpdateInProgress {
    $updateMutex = [Threading.Mutex]::new($false, "Local\LocalAIWorkbenchUpdater")
    $acquired = $false
    try {
        $acquired = $updateMutex.WaitOne(0, $false)
        return -not $acquired
    }
    catch [Threading.AbandonedMutexException] {
        $acquired = $true
        return $false
    }
    finally {
        if ($acquired) {
            try { $updateMutex.ReleaseMutex() } catch { }
        }
        $updateMutex.Dispose()
    }
}

function Show-WorkbenchMessage {
    param(
        [string]$Message,
        [string]$Title = "Local AI Workbench",
        [string]$Button = "OK",
        [string]$Icon = "Information"
    )
    Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
    return [System.Windows.MessageBox]::Show($Message, $Title, $Button, $Icon)
}

function Quote-NativeArgument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Start-DetachedUpdate {
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", (Quote-NativeArgument -Value $updateScript),
        "-Mode", "Apply",
        "-RepositoryRoot", (Quote-NativeArgument -Value $projectRoot),
        "-Restart",
        "-ShowDialogs"
    ) -join " "
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $powershellPath
    $startInfo.Arguments = $arguments
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Windows could not start the Workbench updater."
    }
    Write-BootstrapLog "Detached updater started (PID $($process.Id))."
}

function Start-Workbench {
    if (-not (Test-Path -LiteralPath $startScript)) {
        throw "Workbench runtime launcher was not found at $startScript"
    }
    $parameters = @{
        BackendPort = $BackendPort
        FrontendPort = $FrontendPort
    }
    if ($SmokeTest) { $parameters["SmokeTest"] = $true }
    if ($NoBrowser) { $parameters["NoBrowser"] = $true }
    if ($UpdateResume) { $parameters["UpdateResume"] = $true }
    & $startScript @parameters
    return $LASTEXITCODE
}

try {
    if (-not (Test-Path -LiteralPath $powershellPath)) {
        throw "Windows PowerShell was not found at $powershellPath"
    }
    if (-not $SmokeTest -and -not $UpdateResume -and (Test-WorkbenchUpdateInProgress)) {
        Show-WorkbenchMessage `
            -Title "Local AI Workbench Update" `
            -Icon "Information" `
            -Message "An update is currently being installed. Local AI Workbench will reopen automatically when it is ready." | Out-Null
        exit 0
    }

    if (-not $SkipUpdate -and -not $SmokeTest -and (Test-Path -LiteralPath $updateScript)) {
        $checkOutput = & $powershellPath `
            -NoLogo `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $updateScript `
            -Mode Check `
            -RepositoryRoot $projectRoot `
            -OutputJson 2>$null
        $checkExitCode = $LASTEXITCODE
        $checkJson = @($checkOutput | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }) |
            Select-Object -Last 1
        $check = $null
        if ($checkJson) {
            try { $check = ConvertFrom-Json -InputObject $checkJson -ErrorAction Stop }
            catch { Write-BootstrapLog "Update status JSON could not be parsed." }
        }

        if ($null -ne $check -and $check.status -eq "available") {
            $current = ([string]$check.current_commit).Substring(0, [Math]::Min(8, ([string]$check.current_commit).Length))
            $latest = ([string]$check.remote_commit).Substring(0, [Math]::Min(8, ([string]$check.remote_commit).Length))
            $choice = Show-WorkbenchMessage `
                -Title "Local AI Workbench Update" `
                -Button "YesNo" `
                -Icon "Question" `
                -Message "A GitHub update is available ($current -> $latest, $($check.behind_by) commits).`n`nInstall it now and restart Local AI Workbench?"
            if ($choice -eq [System.Windows.MessageBoxResult]::Yes) {
                Start-DetachedUpdate
                exit 0
            }
        }
        elseif ($null -ne $check -and $check.status -eq "blocked" -and [int]$check.behind_by -gt 0) {
            Show-WorkbenchMessage `
                -Title "Local AI Workbench Update Paused" `
                -Icon "Warning" `
                -Message "A GitHub update exists but cannot be applied safely:`n`n$($check.message)`n`nThe installed version will start normally." | Out-Null
        }
        elseif ($checkExitCode -ne 0) {
            Write-BootstrapLog "Update check was unavailable (exit $checkExitCode); starting the installed version."
        }
    }

    $exitCode = Start-Workbench
    exit $exitCode
}
catch {
    Write-BootstrapLog "ERROR: $($_.Exception.Message)"
    if (-not $SmokeTest) {
        try {
            Show-WorkbenchMessage `
                -Icon "Error" `
                -Message "$($_.Exception.Message)`n`nDetails: $launchLog" | Out-Null
        }
        catch { }
    }
    exit 1
}
