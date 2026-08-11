[CmdletBinding()]
param(
    [switch]$SmokeTest,
    [switch]$NoBrowser,
    [switch]$UpdateResume,
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 8080
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runtimeDir = Join-Path $projectRoot "runtime"
$logDir = Join-Path $runtimeDir "logs"
$browserProfile = Join-Path $runtimeDir "launcher-browser-profile"
$launcherLog = Join-Path $logDir "launcher.log"
$startupProgressScript = Join-Path $projectRoot "backend\startup_progress.py"
$startupServerScript = Join-Path $projectRoot "scripts\startup_http_server.py"
$settingsPath = if ([string]::IsNullOrWhiteSpace($env:WORKBENCH_SETTINGS_PATH)) {
    Join-Path $projectRoot "backend\settings.json"
} else {
    [System.IO.Path]::GetFullPath($env:WORKBENCH_SETTINGS_PATH)
}
$hermesRuntimeDir = Join-Path $projectRoot "runtime\hermes"
$hermesInstallReceipt = Join-Path $hermesRuntimeDir "install-receipt.json"
$hermesManifestPath = Join-Path $projectRoot "config\hermes-sidecar-manifest.json"
$hermesLaunchResolver = Join-Path $projectRoot "scripts\resolve_hermes_launch.py"
$hermesDatabaseRoot = if ([string]::IsNullOrWhiteSpace($env:WORKBENCH_RUNTIME_DIR)) {
    $runtimeDir
} else {
    [System.IO.Path]::GetFullPath($env:WORKBENCH_RUNTIME_DIR)
}
$hermesDatabasePath = Join-Path $hermesDatabaseRoot "db\workbench.db"
$hermesProjectsRoot = Join-Path $projectRoot "projects"
$hermesStartScript = Join-Path $projectRoot "scripts\start_hermes_sidecar.ps1"
$hermesProductionOps = Join-Path $projectRoot "scripts\hermes_production_ops.py"
$backendUrl = "http://127.0.0.1:$BackendPort"
$frontendVersion = "5.13.5-project-skills-hermes"
$websiteUrl = "$backendUrl/index.html?v=$frontendVersion"
$discoveryConfigPath = Join-Path $runtimeDir "server-discovery-config.json"
$discoveryCachePath = Join-Path $runtimeDir "server-discovery-cache.json"
$defaultCandidatePorts = @{
    backend = @(8000, 8001, 8002, 8003, 8004, 8005, 8100, 8101, 8102, 8103)
    frontend = @(8080, 8765, 8766, 8767, 8768, 8769, 8770, 8771, 8772, 8773)
}
$fallbackPortRanges = @{
    backend = 8200..8225
    frontend = 8780..8805
}
$loadingUrl = $null
$backendProcess = $null
$frontendProcess = $null
$backendWorker = $null
$frontendWorker = $null
$browserProcess = $null
$hermesProcess = $null
$hermesLaunchPlan = $null
$hermesHealthFailureCount = 0
$hermesRestartCount = 0
$hermesNextProbeUtc = [DateTime]::MinValue
$hermesEnvironmentManaged = $false
$previousHermesApiServerKey = [Environment]::GetEnvironmentVariable("HERMES_API_SERVER_KEY", "Process")
$jobHandle = [IntPtr]::Zero
$mutex = $null
$hasMutex = $false

function Write-LauncherLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $launcherLog -Value "[$timestamp] $Message" -Encoding UTF8
}

function Test-TcpPortAvailable {
    param([int]$Port)
    # A wildcard/IPv6 listener (for example Docker's :: listener) can reserve the
    # port even when a trial bind to 127.0.0.1 appears to succeed. Check every
    # Windows listening socket first so fallback-port selection is reliable.
    $existingListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $existingListener) { return $false }
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function Get-JsonObjectFromFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        return ConvertFrom-Json -InputObject $raw -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Convert-PsObjectToDictionary {
    param([object]$InputObject)
    if ($null -eq $InputObject) { return @{} }
    if ($InputObject -is [hashtable]) { return $InputObject }
    $result = @{}
    foreach ($property in $InputObject.PSObject.Properties) {
        $result[$property.Name] = $property.Value
    }
    return $result
}

function Get-PortDiscoveryCache {
    return Convert-PsObjectToDictionary -InputObject (Get-JsonObjectFromFile -Path $discoveryCachePath)
}

function Get-PortCandidatesFromConfig {
    param([Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind)

    $candidates = @()
    $config = Get-JsonObjectFromFile -Path $discoveryConfigPath
    if ($null -ne $config -and $config.PSObject.Properties[$Kind]) {
        $candidateNode = $config.PSObject.Properties[$Kind].Value
        foreach ($entry in @($candidateNode)) {
            $parsed = 0
            if ([int]::TryParse([string]$entry, [ref]$parsed)) {
                $candidates += $parsed
            }
        }
    }
    if ($candidates.Count -eq 0 -and $defaultCandidatePorts.ContainsKey($Kind)) {
        $candidates = $defaultCandidatePorts[$Kind]
    }
    $envName = "WORKBENCH_{0}_CANDIDATE_PORTS" -f $Kind.ToUpper()
    $rawFromEnv = [Environment]::GetEnvironmentVariable($envName)
    if ($null -ne $rawFromEnv) {
        foreach ($entry in ($rawFromEnv -split "[,;]")) {
            $parsed = 0
            if ([int]::TryParse($entry.Trim(), [ref]$parsed)) {
                $candidates += $parsed
            }
        }
    }
    return $candidates | Where-Object { $_ -ge 1024 -and $_ -le 65535 } | Select-Object -Unique
}

function Get-CachedCandidatePorts {
    param([Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind)

    $cache = Get-PortDiscoveryCache
    if (-not $cache.ContainsKey($Kind)) { return @() }
    $entry = $cache[$Kind]
    if ($null -eq $entry -or -not $entry.PSObject.Properties["available_ports"]) { return @() }
    $ports = @()
    foreach ($entryPort in @($entry.available_ports)) {
        $parsed = 0
        if ([int]::TryParse([string]$entryPort, [ref]$parsed)) {
            $ports += $parsed
        }
    }
    return $ports | Where-Object { $_ -ge 1024 -and $_ -le 65535 } | Select-Object -Unique
}

function Test-ServiceHealthy {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind,
        [Parameter(Mandatory = $true)] [int]$Port
    )
    try {
        if ($Kind -eq "backend") {
            $healthUrl = "http://127.0.0.1:$Port/api/health"
            $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
            return $response.success -eq $true
        }
        $healthUrl = "http://127.0.0.1:$Port/loading.html?v=$frontendVersion"
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-DiscoveryCandidatePorts {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind,
        [Parameter(Mandatory = $true)] [int]$RequestedPort
    )
    $configPorts = Get-PortCandidatesFromConfig -Kind $Kind
    $cachedPorts = Get-CachedCandidatePorts -Kind $Kind
    $ordered = @($RequestedPort) + $cachedPorts + $configPorts
    return $ordered | Where-Object { $_ -ge 1024 -and $_ -le 65535 } | Select-Object -Unique
}

function Resolve-ServicePort {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind,
        [Parameter(Mandatory = $true)] [int]$RequestedPort
    )
    $candidates = Get-DiscoveryCandidatePorts -Kind $Kind -RequestedPort $RequestedPort
    $checked = @()
    $candidateHit = $false
    $healthOkCount = 0
    foreach ($candidate in $candidates) {
        $checked += $candidate
        if (Test-TcpPortAvailable -Port $candidate) {
            $candidateHit = $true
            return @{
                port = $candidate
                checked = $checked
                source = "candidate_pool"
                candidate_hit = $candidateHit
                health_ok = $healthOkCount
                fallback_used = $false
            }
        }
        if (Test-ServiceHealthy -Kind $Kind -Port $candidate) {
            $healthOkCount += 1
            Write-LauncherLog "$Kind service is already healthy on port $candidate; continue checking fallback candidates."
        }
    }
    foreach ($candidate in $fallbackPortRanges[$Kind]) {
        if ($checked -contains $candidate) { continue }
        $checked += $candidate
        if (Test-TcpPortAvailable -Port $candidate) {
            return @{
                port = $candidate
                checked = $checked
                source = "localhost_scan"
                candidate_hit = $candidateHit
                health_ok = $healthOkCount
                fallback_used = $true
            }
        }
    }
    throw "No available $Kind port found in discovery candidates or localhost scan."
}

function Write-PortDiscoverySummary {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind,
        [Parameter(Mandatory = $true)] [object]$Plan
    )
    $checked = @($Plan.checked)
    Write-LauncherLog ("{0} discovery summary: candidate_hit={1}; health_ok={2}; fallback_used={3}; source={4}; checked_ports={5}; checked_ports_count={6}; chosen_port={7}" -f `
        $Kind, `
        [bool]$Plan.candidate_hit, `
        [int]$Plan.health_ok, `
        [bool]$Plan.fallback_used, `
        [string]$Plan.source, `
        ($checked -join ","), `
        $checked.Count, `
        [int]$Plan.port)
}

function Update-PortDiscoveryCache {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("backend", "frontend")] [string]$Kind,
        [Parameter(Mandatory = $true)] [int]$ChosenPort,
        [Parameter(Mandatory = $true)] [object]$CheckedPorts
    )
    $cache = Get-PortDiscoveryCache
    if (-not ($cache -is [hashtable])) { $cache = Convert-PsObjectToDictionary -InputObject $cache }
    $cache[$Kind] = [ordered]@{
        last_checked_utc = (Get-Date).ToUniversalTime().ToString("o")
        chosen_port = $ChosenPort
        available_ports = @($CheckedPorts | Where-Object { $_ -is [int] })
    }
    try {
        $cache | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $discoveryCachePath
    }
    catch {
        Write-LauncherLog "Unable to update discovery cache: $($_.Exception.Message)"
    }
}

function Wait-HttpReady {
    param([string]$Url, [int]$TimeoutSeconds = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return $true }
        }
        catch {
            Start-Sleep -Milliseconds 400
        }
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Wait-BackendReady {
    param(
        [string]$Url,
        [Diagnostics.Process]$ServiceProcess,
        [Diagnostics.Process]$WebsiteProcess,
        [int]$TimeoutSeconds = 90
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ($null -ne $WebsiteProcess -and $WebsiteProcess.HasExited) { return "window_closed" }
        if ($null -ne $ServiceProcess -and $ServiceProcess.HasExited) { return "service_stopped" }
        try {
            $response = Invoke-RestMethod -Uri $Url -TimeoutSec 3
            if ($response.success -eq $true) { return "ready" }
        }
        catch { }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)
    return "timeout"
}

function Test-ExistingBackend {
    try {
        $result = Invoke-RestMethod -Uri "$backendUrl/api/health" -TimeoutSec 5
        return $result.success -eq $true
    }
    catch { return $false }
}

function Test-ExistingFrontend {
    try {
        $result = Invoke-WebRequest -UseBasicParsing -Uri $websiteUrl -TimeoutSec 5
        return $result.StatusCode -eq 200 -and $result.Content -match "<title>Local AI Workbench</title>"
    }
    catch { return $false }
}

function Get-LoopbackListenerProcess {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $connection) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
}

function Stop-RecognizedWorkbenchService {
    param(
        [int]$Port,
        [ValidateSet("backend", "frontend")]
        [string]$Kind
    )
    $process = Get-LoopbackListenerProcess -Port $Port
    if ($null -eq $process) { return $true }
    $command = [string]$process.CommandLine
    $recognized = $process.Name -eq "python.exe"
    if ($Kind -eq "backend") {
        $recognized = $recognized -and $command -match "uvicorn\s+app:app" -and $command -match "--app-dir\s+backend" -and $command -match "--port\s+$Port(?:\s|$)"
    }
    else {
        $legacyFrontend = $command -match "http\.server\s+$Port(?:\s|$)" -and $command -match "--directory\s+frontend"
        $startupFrontend = $command -match "startup_http_server\.py" -and $command -match "--port\s+$Port(?:\s|$)"
        $recognized = $recognized -and ($legacyFrontend -or $startupFrontend)
    }
    if (-not $recognized) { return $false }
    Write-LauncherLog "Stopping orphaned $Kind service PID $($process.ProcessId) so the current code can be loaded."
    Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-TcpPortAvailable -Port $Port) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    return Test-TcpPortAvailable -Port $Port
}

function Initialize-KillOnCloseJob {
    if (-not ("Workbench.NativeMethods" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace Workbench {
    public static class NativeMethods {
        [StructLayout(LayoutKind.Sequential)]
        public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
            public Int64 PerProcessUserTimeLimit; public Int64 PerJobUserTimeLimit;
            public UInt32 LimitFlags; public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize; public UInt32 ActiveProcessLimit;
            public UIntPtr Affinity; public UInt32 PriorityClass; public UInt32 SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        public struct IO_COUNTERS {
            public UInt64 ReadOperationCount; public UInt64 WriteOperationCount;
            public UInt64 OtherOperationCount; public UInt64 ReadTransferCount;
            public UInt64 WriteTransferCount; public UInt64 OtherTransferCount;
        }
        [StructLayout(LayoutKind.Sequential)]
        public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo; public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit; public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll")]
        public static extern bool SetInformationJobObject(IntPtr job, int infoType, IntPtr info, UInt32 length);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
        [DllImport("kernel32.dll")]
        public static extern bool CloseHandle(IntPtr handle);
    }
}
"@
    }
    $handle = [Workbench.NativeMethods]::CreateJobObject([IntPtr]::Zero, $null)
    if ($handle -eq [IntPtr]::Zero) { throw "Unable to create the process cleanup job." }
    $info = New-Object Workbench.NativeMethods+JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    $info.BasicLimitInformation.LimitFlags = 0x00002000
    $length = [Runtime.InteropServices.Marshal]::SizeOf($info)
    $pointer = [Runtime.InteropServices.Marshal]::AllocHGlobal($length)
    try {
        [Runtime.InteropServices.Marshal]::StructureToPtr($info, $pointer, $false)
        if (-not [Workbench.NativeMethods]::SetInformationJobObject($handle, 9, $pointer, $length)) {
            throw "Unable to configure the process cleanup job."
        }
    }
    finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($pointer) }
    return $handle
}

function Add-ProcessToJob {
    param([IntPtr]$Job, [System.Diagnostics.Process]$Process)
    $alreadyRegistered = $false
    if (-not [Workbench.NativeMethods]::IsProcessInJob($Process.Handle, $Job, [ref]$alreadyRegistered)) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "Unable to inspect process $($Process.Id) cleanup registration (Windows error $errorCode)."
    }
    if ($alreadyRegistered) {
        Write-LauncherLog "Process $($Process.Id) already inherited automatic cleanup registration."
        return
    }
    if (-not [Workbench.NativeMethods]::AssignProcessToJobObject($Job, $Process.Handle)) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "Unable to register process $($Process.Id) for automatic cleanup (Windows error $errorCode)."
    }
}

function Find-Browser {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe")
    )
    return $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Get-LauncherBrowserProcess {
    $escapedProfile = [Regex]::Escape($browserProfile)
    $candidate = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @("msedge.exe", "chrome.exe") -and
            [string]$_.CommandLine -match "(?i)--user-data-dir=(?:`"|)$escapedProfile(?:`"|\s|$)" -and
            [string]$_.CommandLine -match "(?i)--app=http://127\.0\.0\.1:" -and
            [string]$_.CommandLine -notmatch "(?i)\s--type="
        } |
        Sort-Object CreationDate -Descending |
        Select-Object -First 1
    if ($null -eq $candidate) { return $null }
    return Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
}

function Stop-StaleLauncherBrowser {
    $stale = Get-LauncherBrowserProcess
    if ($null -eq $stale) { return }
    Write-LauncherLog "Closing stale launcher browser PID $($stale.Id) before opening the current startup URL."
    Stop-Process -Id $stale.Id -Force -ErrorAction SilentlyContinue
    try { $stale.WaitForExit(5000) | Out-Null } catch { }
}

function Resolve-LaunchedBrowserProcess {
    param(
        [System.Diagnostics.Process]$InitialProcess,
        [int]$TimeoutSeconds = 8
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ($null -ne $InitialProcess -and -not $InitialProcess.HasExited) {
            $InitialProcess.Refresh()
            if ($InitialProcess.MainWindowHandle -ne [IntPtr]::Zero) { return $InitialProcess }
        }
        $adopted = Get-LauncherBrowserProcess
        if ($null -ne $adopted -and -not $adopted.HasExited) {
            $adopted.Refresh()
            if ($adopted.MainWindowHandle -ne [IntPtr]::Zero) {
                if ($null -eq $InitialProcess -or $adopted.Id -ne $InitialProcess.Id) {
                    Write-LauncherLog "Browser startup was handed off; tracking launcher window PID $($adopted.Id)."
                }
                return $adopted
            }
        }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return $null
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

function Find-HealthyWorkbenchBackendPort {
    param([int]$RequestedPort)

    $candidates = @(
        Get-DiscoveryCandidatePorts -Kind "backend" -RequestedPort $RequestedPort
    ) + @($fallbackPortRanges["backend"])
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-ServiceHealthy -Kind "backend" -Port ([int]$candidate)) {
            return [int]$candidate
        }
    }
    return $null
}

function Open-ExistingWorkbenchWindow {
    param([int]$RequestedPort)

    $existingBrowser = Get-LauncherBrowserProcess
    if ($null -ne $existingBrowser -and -not $existingBrowser.HasExited) {
        $existingBrowser.Refresh()
        if ($existingBrowser.MainWindowHandle -ne [IntPtr]::Zero) {
            try {
                Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
                if ([Microsoft.VisualBasic.Interaction]::AppActivate($existingBrowser.Id)) {
                    Write-LauncherLog "Existing workbench window activated (PID $($existingBrowser.Id))."
                    return $true
                }
            }
            catch {
                Write-LauncherLog "Existing window activation warning: $($_.Exception.Message)"
            }
        }
    }

    $healthyPort = Find-HealthyWorkbenchBackendPort -RequestedPort $RequestedPort
    if ($null -eq $healthyPort) {
        Write-LauncherLog "Existing launcher detected, but no healthy workbench backend was found."
        return $false
    }
    $browserPath = Find-Browser
    if (-not $browserPath) {
        Write-LauncherLog "Existing launcher detected, but Microsoft Edge or Google Chrome was not found."
        return $false
    }

    $existingUrl = "http://127.0.0.1:$healthyPort/index.html?v=$frontendVersion"
    $browserArgs = @(
        "--app=$existingUrl",
        "--window-size=1920,1080",
        "--user-data-dir=$browserProfile",
        "--new-window",
        "--no-first-run",
        "--disable-background-mode",
        "--disable-extensions"
    )
    $startedBrowser = Start-Process -FilePath $browserPath -ArgumentList $browserArgs -PassThru
    $openedBrowser = Resolve-LaunchedBrowserProcess -InitialProcess $startedBrowser -TimeoutSeconds 8
    if ($null -eq $openedBrowser) {
        Write-LauncherLog "Existing backend was healthy on port $healthyPort, but its workbench window could not be tracked."
        return $false
    }
    Write-LauncherLog "Opened a workbench window for the existing healthy backend on port $healthyPort."
    return $true
}

function Get-HeadlessWorkbenchLauncher {
    $launcherScript = Join-Path $PSScriptRoot "start_workbench.ps1"
    $escapedLauncherScript = [Regex]::Escape($launcherScript)
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.Name -in @("powershell.exe", "pwsh.exe") -and
            [string]$_.CommandLine -match "(?i)$escapedLauncherScript" -and
            [string]$_.CommandLine -match "(?i)(?:^|\s)-NoBrowser(?:\s|$)"
        } |
        Sort-Object CreationDate |
        Select-Object -First 1
}

function Wait-ForLauncherMutex {
    param(
        [Parameter(Mandatory = $true)] [Threading.Mutex]$Mutex,
        [int]$TimeoutMilliseconds
    )
    try {
        return $Mutex.WaitOne($TimeoutMilliseconds, $false)
    }
    catch [Threading.AbandonedMutexException] {
        return $true
    }
}

function Get-ServiceWorker {
    param(
        [System.Diagnostics.Process]$LauncherProcess,
        [int]$Port
    )
    $deadline = (Get-Date).AddSeconds(5)
    do {
        $child = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($LauncherProcess.Id)" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -match "(?:--port\s+|http\.server\s+)$Port(?:\s|$)" } |
            Select-Object -First 1
        if ($null -eq $child) {
            $listenerPattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
            $listenerLine = netstat.exe -ano -p tcp | Where-Object { $_ -match $listenerPattern } | Select-Object -First 1
            $listenerProcess = if ($listenerLine -and $listenerLine -match $listenerPattern) {
                Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
            } else { $null }
            if ($null -ne $listenerProcess -and $listenerProcess.ProcessName -eq "python") { return $listenerProcess }
        }
        if ($null -eq $child) { Start-Sleep -Milliseconds 100 }
    } while ($null -eq $child -and (Get-Date) -lt $deadline)
    if ($null -eq $child) { throw "Could not identify the service process on port $Port." }
    return Get-Process -Id $child.ProcessId -ErrorAction Stop
}

function Stop-OwnedProcess {
    param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            $Process.WaitForExit(5000) | Out-Null
        }
    }
    catch { Write-LauncherLog "Cleanup warning for process $($Process.Id): $($_.Exception.Message)" }
}

function Stop-HermesSidecar {
    param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }

    $deploymentMode = ""
    $containerName = ""
    $toolPolicyProfile = ""
    if ($Process.PSObject.Properties["HermesDeploymentMode"]) {
        $deploymentMode = [string]$Process.PSObject.Properties["HermesDeploymentMode"].Value
    }
    if ($Process.PSObject.Properties["HermesContainerName"]) {
        $containerName = [string]$Process.PSObject.Properties["HermesContainerName"].Value
    }
    if ($Process.PSObject.Properties["HermesToolPolicyProfile"]) {
        $toolPolicyProfile = [string]$Process.PSObject.Properties["HermesToolPolicyProfile"].Value
    }
    if ($deploymentMode -eq "docker" -and
        $containerName -eq "local-ai-workbench-hermes" -and
        $toolPolicyProfile -in @("NoTools", "ProjectReadOnly") -and
        (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
        try {
            $containerIdOutput = @(& docker.exe container inspect --format "{{.Id}}" $containerName 2>&1)
            if ($LASTEXITCODE -eq 0) {
                $containerId = [string]($containerIdOutput | Select-Object -First 1)
                if ($containerId -notmatch '^[0-9a-f]{64}$') {
                    Write-LauncherLog "Cleanup warning: Hermes returned an invalid owned container identity."
                    return
                }
                # Pin the immutable ID before checking labels so a name-reuse
                # race can never redirect cleanup to another container.
                $inspectOutput = @(& docker.exe container inspect --format "{{json .Config.Labels}}" $containerId 2>&1)
                if ($LASTEXITCODE -ne 0) { return }
                $labels = (($inspectOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop)
                $owner = $labels.PSObject.Properties["com.local-ai-workbench.owner"]
                $component = $labels.PSObject.Properties["com.local-ai-workbench.component"]
                $policy = $labels.PSObject.Properties["com.local-ai-workbench.policy"]
                $expectedPolicy = if ($toolPolicyProfile -eq "ProjectReadOnly") { "project-readonly-v1" } else { "no-tools-v1" }
                if ($null -ne $owner -and $null -ne $component -and
                    $null -ne $policy -and
                    [string]$owner.Value -eq "workbench" -and
                    [string]$component.Value -eq "hermes-sidecar" -and
                    [string]$policy.Value -eq $expectedPolicy) {
                    $null = & docker.exe stop --time 10 $containerId 2>&1
                    if ($LASTEXITCODE -ne 0) {
                        Write-LauncherLog "Cleanup warning: the owned Hermes Docker container did not stop cleanly."
                    }
                    $remaining = @(& docker.exe container ls --all --filter "id=$containerId" --format "{{.ID}}" 2>&1)
                    if ($LASTEXITCODE -eq 0 -and
                        @($remaining | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }).Count -ne 0) {
                        $null = & docker.exe container rm $containerId 2>&1
                        if ($LASTEXITCODE -ne 0) {
                            Write-LauncherLog "Cleanup warning: the stopped owned Hermes container was not removed."
                        }
                    }
                }
                else {
                    Write-LauncherLog "Cleanup warning: Hermes container ownership or policy labels changed; refusing to stop it."
                }
            }
        }
        catch {
            Write-LauncherLog "Cleanup warning for the Hermes Docker container: $($_.Exception.Message)"
        }
    }
    Stop-OwnedProcess -Process $Process
}

function Get-HermesLaunchPlan {
    if (-not (Test-Path -LiteralPath $hermesLaunchResolver -PathType Leaf)) {
        throw "Hermes launch-plan resolver is missing: $hermesLaunchResolver"
    }
    $planOutput = @(& $pythonPath $hermesLaunchResolver `
        --settings $settingsPath `
        --receipt $hermesInstallReceipt `
        --manifest $hermesManifestPath `
        --database $hermesDatabasePath `
        --projects-root $hermesProjectsRoot 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $planFailure = ($planOutput -join " ").Trim()
        if ([string]::IsNullOrWhiteSpace($planFailure)) {
            $planFailure = "The persisted Hermes launch inputs failed validation."
        }
        throw $planFailure
    }
    try {
        $plan = (($planOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "Hermes launch-plan resolver returned invalid output."
    }
    if ($null -eq $plan.PSObject.Properties["enabled"] -or $plan.enabled -isnot [bool]) {
        throw "Hermes launch-plan resolver returned an invalid enabled state."
    }
    if ([bool]$plan.enabled) {
        $monitoring = $plan.PSObject.Properties["monitoring"]
        if ($null -eq $monitoring -or
            [int]$monitoring.Value.probe_interval_seconds -ne 10 -or
            [int]$monitoring.Value.failure_threshold -ne 3 -or
            [int]$monitoring.Value.max_restarts_per_launch -ne 2 -or
            [int]$monitoring.Value.restart_backoff_seconds -ne 2) {
            throw "Hermes launch-plan resolver returned an unsupported monitoring policy."
        }
    }
    return $plan
}

function Start-ManagedHermesSidecar {
    $script:hermesLaunchPlan = Get-HermesLaunchPlan
    if (-not [bool]$script:hermesLaunchPlan.enabled) {
        $script:hermesProcess = $null
        $script:hermesHealthFailureCount = 0
        $script:hermesNextProbeUtc = [DateTime]::MinValue
        return
    }
    if (-not (Test-Path -LiteralPath $hermesStartScript -PathType Leaf)) {
        throw "Hermes is enabled and installed, but its sidecar launcher is missing: $hermesStartScript"
    }
    $deploymentMode = [string]$script:hermesLaunchPlan.deployment_mode
    $toolPolicyProfile = [string]$script:hermesLaunchPlan.tool_policy_profile
    if ($deploymentMode -notin @("Native", "Docker") -or
        $toolPolicyProfile -notin @("NoTools", "ProjectReadOnly") -or
        ($toolPolicyProfile -eq "ProjectReadOnly" -and $deploymentMode -ne "Docker")) {
        throw "Hermes launch-plan resolver returned an unsupported deployment policy."
    }
    $startParameters = @{
        DeploymentMode = $deploymentMode
        ToolPolicyProfile = $toolPolicyProfile
        PassThru = $true
    }
    if ($toolPolicyProfile -eq "ProjectReadOnly") {
        $resolvedProjectId = [string]$script:hermesLaunchPlan.project_id
        $resolvedProjectRoot = [string]$script:hermesLaunchPlan.project_root
        if ([string]::IsNullOrWhiteSpace($resolvedProjectId) -or
            [string]::IsNullOrWhiteSpace($resolvedProjectRoot)) {
            throw "Hermes read-only launch plan did not resolve one project."
        }
        $startParameters["ProjectId"] = $resolvedProjectId
        $startParameters["ProjectRoot"] = $resolvedProjectRoot
    }
    Write-LauncherLog "Hermes is enabled and verified; starting the isolated $($deploymentMode.ToLowerInvariant()) sidecar with policy $toolPolicyProfile."
    $script:hermesEnvironmentManaged = $true
    # Invoke in this PowerShell process so the fixed key alias is inherited by
    # the backend. The sidecar script performs the complete readiness gate.
    $startOutput = @(& $hermesStartScript @startParameters)
    $script:hermesProcess = $startOutput |
        Where-Object { $_ -is [System.Diagnostics.Process] } |
        Select-Object -Last 1
    if ($null -eq $script:hermesProcess) {
        throw "Hermes sidecar launcher did not return its owned process."
    }
    if ([string]::IsNullOrWhiteSpace($env:HERMES_API_SERVER_KEY)) {
        throw "Hermes sidecar did not publish HERMES_API_SERVER_KEY to the backend environment."
    }
    Add-ProcessToJob -Job $jobHandle -Process $script:hermesProcess
    $script:hermesHealthFailureCount = 0
    $script:hermesNextProbeUtc = [DateTime]::UtcNow.AddSeconds(
        [int]$script:hermesLaunchPlan.monitoring.probe_interval_seconds
    )
    Write-LauncherLog "Hermes $($deploymentMode.ToLowerInvariant()) sidecar is ready on 127.0.0.1:8642 (PID $($script:hermesProcess.Id))."
}

function Test-HermesRuntimeReady {
    if ($null -eq $script:hermesProcess) { return $false }
    try {
        $script:hermesProcess.Refresh()
        if ($script:hermesProcess.HasExited -or [string]::IsNullOrWhiteSpace($env:HERMES_API_SERVER_KEY)) {
            return $false
        }
        $headers = @{ Authorization = "Bearer $env:HERMES_API_SERVER_KEY" }
        $health = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8642/health" -Headers $headers -TimeoutSec 3
        if ([string]$health.status -ne "ok" -or
            [string]$health.platform -ne "hermes-agent" -or
            [string]$health.version -ne "0.18.2") {
            return $false
        }
        $capabilities = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8642/v1/capabilities" -Headers $headers -TimeoutSec 3
        if ([string]$capabilities.object -ne "hermes.api_server.capabilities" -or
            [string]$capabilities.platform -ne "hermes-agent" -or
            [string]$capabilities.auth.type -ne "bearer" -or
            $capabilities.auth.required -isnot [bool] -or -not [bool]$capabilities.auth.required -or
            [string]$capabilities.runtime.mode -ne "server_agent" -or
            [string]$capabilities.runtime.tool_execution -ne "server" -or
            $capabilities.runtime.split_runtime -isnot [bool] -or [bool]$capabilities.runtime.split_runtime) {
            return $false
        }
        foreach ($feature in @("run_approval_response", "run_events_sse", "run_status", "run_stop", "run_submission")) {
            $featureProperty = $capabilities.features.PSObject.Properties[$feature]
            if ($null -eq $featureProperty -or $featureProperty.Value -isnot [bool] -or -not [bool]$featureProperty.Value) {
                return $false
            }
        }
        $expectedEndpoints = @{
            runs = @("POST", "/v1/runs")
            run_status = @("GET", "/v1/runs/{run_id}")
            run_events = @("GET", "/v1/runs/{run_id}/events")
            run_approval = @("POST", "/v1/runs/{run_id}/approval")
            run_stop = @("POST", "/v1/runs/{run_id}/stop")
        }
        foreach ($endpointName in $expectedEndpoints.Keys) {
            $endpointProperty = $capabilities.endpoints.PSObject.Properties[$endpointName]
            if ($null -eq $endpointProperty -or
                [string]$endpointProperty.Value.method -ne [string]$expectedEndpoints[$endpointName][0] -or
                [string]$endpointProperty.Value.path -ne [string]$expectedEndpoints[$endpointName][1]) {
                return $false
            }
        }
        $toolsets = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8642/v1/toolsets" -Headers $headers -TimeoutSec 3
        if ([string]$toolsets.object -ne "list" -or [string]$toolsets.platform -ne "api_server" -or $null -eq $toolsets.data) {
            return $false
        }
        $enabledToolsets = New-Object System.Collections.Generic.List[object]
        foreach ($entry in @($toolsets.data)) {
            if ($entry.enabled -isnot [bool] -or $null -eq $entry.name -or $null -eq $entry.tools) {
                return $false
            }
            if ([bool]$entry.enabled) { $enabledToolsets.Add($entry) }
        }
        $profileProperty = $script:hermesProcess.PSObject.Properties["HermesToolPolicyProfile"]
        if ($null -eq $profileProperty) { return $false }
        $profile = [string]$profileProperty.Value
        if ($profile -eq "NoTools") {
            return $enabledToolsets.Count -eq 0
        }
        if ($profile -ne "ProjectReadOnly" -or $enabledToolsets.Count -ne 1 -or
            [string]$enabledToolsets[0].name -ne "workbench-readonly") {
            return $false
        }
        $actualTools = @($enabledToolsets[0].tools | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        return ($actualTools -join ",") -eq "project_read_file,project_search_files"
    }
    catch {
        return $false
    }
}

function Write-HermesProductionEvidence {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("launcher-health-threshold", "launcher-restart-succeeded", "launcher-restart-exhausted")]
        [string]$Operation,
        [ValidateRange(0, 2)]
        [int]$RestartCount
    )

    if (-not (Test-Path -LiteralPath $hermesProductionOps -PathType Leaf)) {
        throw "Hermes production evidence writer is missing."
    }
    # The event interface accepts only a fixed operation and bounded counter.
    # Remove the process API key while launching it; no environment, project,
    # or session material is passed as an evidence argument.
    $currentApiKey = [Environment]::GetEnvironmentVariable("HERMES_API_SERVER_KEY", "Process")
    try {
        [Environment]::SetEnvironmentVariable("HERMES_API_SERVER_KEY", $null, "Process")
        $null = @(& $pythonPath $hermesProductionOps $Operation --restart-count $RestartCount 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Hermes production evidence could not be recorded."
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable("HERMES_API_SERVER_KEY", $currentApiKey, "Process")
    }
}

function Invoke-HermesMonitorTick {
    if ($null -eq $script:hermesLaunchPlan -or -not [bool]$script:hermesLaunchPlan.enabled) { return }
    if ([DateTime]::UtcNow -lt $script:hermesNextProbeUtc) { return }
    $script:hermesNextProbeUtc = [DateTime]::UtcNow.AddSeconds(
        [int]$script:hermesLaunchPlan.monitoring.probe_interval_seconds
    )
    if (Test-HermesRuntimeReady) {
        $script:hermesHealthFailureCount = 0
        return
    }
    $script:hermesHealthFailureCount += 1
    $failureThreshold = [int]$script:hermesLaunchPlan.monitoring.failure_threshold
    Write-LauncherLog "Hermes health probe failed ($($script:hermesHealthFailureCount)/$failureThreshold)."
    if ($script:hermesHealthFailureCount -lt $failureThreshold) { return }

    $restartLimit = [int]$script:hermesLaunchPlan.monitoring.max_restarts_per_launch
    if ($script:hermesRestartCount -ge $restartLimit) {
        Write-HermesProductionEvidence -Operation "launcher-restart-exhausted" -RestartCount $script:hermesRestartCount
        throw "Hermes failed its reviewed health gate after $restartLimit owned restart attempts."
    }
    $script:hermesRestartCount += 1
    Write-HermesProductionEvidence -Operation "launcher-health-threshold" -RestartCount $script:hermesRestartCount
    Write-LauncherLog "Hermes reached its health-failure threshold; restarting the owned sidecar ($($script:hermesRestartCount)/$restartLimit)."
    $stoppedProcess = $script:hermesProcess
    Stop-HermesSidecar -Process $stoppedProcess
    $script:hermesProcess = $null
    Start-Sleep -Seconds ([int]$script:hermesLaunchPlan.monitoring.restart_backoff_seconds)
    # Re-resolve persisted settings, receipt, project scope, and policy before
    # every restart. A disabled plan remains stopped and cannot retain tools.
    Start-ManagedHermesSidecar
    if ($null -ne $script:hermesLaunchPlan -and [bool]$script:hermesLaunchPlan.enabled) {
        Write-HermesProductionEvidence -Operation "launcher-restart-succeeded" -RestartCount $script:hermesRestartCount
    }
}

try {
    New-Item -ItemType Directory -Force -Path $logDir, $browserProfile | Out-Null
    if (-not $SmokeTest -and -not $UpdateResume -and (Test-WorkbenchUpdateInProgress)) {
        throw "A Local AI Workbench update is in progress. Start the application again after the update finishes."
    }
    $mutex = [Threading.Mutex]::new($false, "Local\LlmWorkbenchLauncher")
    $hasMutex = Wait-ForLauncherMutex -Mutex $mutex -TimeoutMilliseconds 0
    if (-not $hasMutex) {
        if (-not $SmokeTest -and -not $NoBrowser) {
            $headlessLauncher = Get-HeadlessWorkbenchLauncher
            if ($null -ne $headlessLauncher) {
                Write-LauncherLog "GUI launch is taking over headless launcher PID $($headlessLauncher.ProcessId)."
                $headlessProcess = Get-Process -Id $headlessLauncher.ProcessId -ErrorAction SilentlyContinue
                if ($null -ne $headlessProcess) {
                    Stop-Process -Id $headlessProcess.Id -Force -ErrorAction SilentlyContinue
                    try { $headlessProcess.WaitForExit(10000) | Out-Null } catch { }
                }
                $hasMutex = Wait-ForLauncherMutex -Mutex $mutex -TimeoutMilliseconds 15000
                if ($hasMutex) {
                    Write-LauncherLog "Headless launcher stopped; continuing with an interactive workbench launch."
                }
            }
            if (-not $hasMutex) {
                if (-not (Open-ExistingWorkbenchWindow -RequestedPort $BackendPort)) {
                    Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
                    [System.Windows.MessageBox]::Show(
                        "Local AI Workbench 正在背景啟動，但目前找不到可開啟的服務。請稍候數秒後再試。`n`nDetails: $launcherLog",
                        "Local AI Workbench",
                        "OK",
                        "Warning"
                    ) | Out-Null
                }
                exit 0
            }
        }
        else {
            if ($SmokeTest) {
                throw "Smoke test could not run because the workbench launcher mutex is already held."
            }
            Write-LauncherLog "Start ignored because the workbench launcher is already running."
            exit 0
        }
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Python virtual environment was not found at $pythonPath" }
    if ($BackendPort -eq $FrontendPort) { throw "BackendPort and FrontendPort must be different." }
    $backendPlan = Resolve-ServicePort -Kind "backend" -RequestedPort $BackendPort
    $BackendPort = [int]$backendPlan.port
    Write-PortDiscoverySummary -Kind "backend" -Plan $backendPlan
    if ($backendPlan.source -eq "candidate_pool") {
        Write-LauncherLog "Backend port resolution: using configured/cached candidate $BackendPort."
    }
    else {
        Write-LauncherLog "Backend port resolution: using localhost scan port $BackendPort."
    }
    if (-not (Test-TcpPortAvailable -Port $BackendPort)) {
        if (-not (Stop-RecognizedWorkbenchService -Port $BackendPort -Kind "backend")) {
            throw "Port $BackendPort is occupied by a service that cannot be safely replaced."
        }
    }
    if (-not (Test-TcpPortAvailable -Port $BackendPort)) {
        throw "Backend port $BackendPort is still unavailable after recognized-service cleanup."
    }
    Update-PortDiscoveryCache -Kind "backend" -ChosenPort $BackendPort -CheckedPorts $backendPlan.checked

    $frontendPlan = Resolve-ServicePort -Kind "frontend" -RequestedPort $FrontendPort
    $FrontendPort = [int]$frontendPlan.port
    $frontendCheckedPorts = @($frontendPlan.checked)
    Write-PortDiscoverySummary -Kind "frontend" -Plan $frontendPlan
    if ($frontendPlan.source -eq "candidate_pool") {
        Write-LauncherLog "Frontend port resolution: using configured/cached candidate $FrontendPort."
    }
    else {
        Write-LauncherLog "Frontend port resolution: using localhost scan port $FrontendPort."
    }
    if (-not (Test-TcpPortAvailable -Port $FrontendPort)) {
        if (-not (Stop-RecognizedWorkbenchService -Port $FrontendPort -Kind "frontend")) {
            $fallbackPort = 8765..8775 | Where-Object { Test-TcpPortAvailable -Port $_ } | Select-Object -First 1
            if ($null -eq $fallbackPort) {
                throw "Frontend port $FrontendPort is occupied and no fallback port is available."
            }
            Write-LauncherLog "Frontend port $FrontendPort is occupied by another service; using $fallbackPort instead."
            $FrontendPort = [int]$fallbackPort
            if ($frontendCheckedPorts -notcontains $FrontendPort) { $frontendCheckedPorts += $FrontendPort }
            $websiteUrl = "$backendUrl/index.html?v=$frontendVersion"
        }
    }
    if (-not (Test-TcpPortAvailable -Port $FrontendPort)) {
        throw "Frontend port $FrontendPort is still unavailable after fallback handling."
    }
    Update-PortDiscoveryCache -Kind "frontend" -ChosenPort $FrontendPort -CheckedPorts $frontendCheckedPorts
    $backendUrl = "http://127.0.0.1:$BackendPort"
    $websiteUrl = "$backendUrl/index.html?v=$frontendVersion"
    $encodedBackendUrl = [Uri]::EscapeDataString($backendUrl)
    $encodedTarget = [Uri]::EscapeDataString($websiteUrl)
    $loadingUrl = "http://127.0.0.1:$FrontendPort/loading.html?v=$frontendVersion&backend=$encodedBackendUrl&target=$encodedTarget"

    $jobHandle = Initialize-KillOnCloseJob
    $backendOut = Join-Path $logDir "backend.stdout.log"
    $backendErr = Join-Path $logDir "backend.stderr.log"
    $frontendOut = Join-Path $logDir "frontend.stdout.log"
    $frontendErr = Join-Path $logDir "frontend.stderr.log"

    $env:WORKBENCH_STARTUP_RUN_ID = "startup_$([Guid]::NewGuid().ToString('N'))"
    $env:WORKBENCH_STARTUP_RECORD_HISTORY = if ($SmokeTest) { "0" } else { "1" }
    & $pythonPath $startupProgressScript begin | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Unable to initialize startup progress." }

    $frontendArgs = @($startupServerScript, "--port", "$FrontendPort", "--bind", "127.0.0.1", "--directory", (Join-Path $projectRoot "frontend"), "--backend-directory", (Join-Path $projectRoot "backend"))
    $frontendProcess = Start-Process -FilePath $pythonPath -ArgumentList $frontendArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -PassThru
    Add-ProcessToJob -Job $jobHandle -Process $frontendProcess
    $frontendProcess.PriorityClass = [Diagnostics.ProcessPriorityClass]::BelowNormal

    if (-not (Wait-HttpReady -Url $loadingUrl -TimeoutSeconds 20)) { throw "The startup screen did not become ready. See $frontendErr" }
    if ($null -ne $frontendProcess) { $frontendWorker = Get-ServiceWorker -LauncherProcess $frontendProcess -Port $FrontendPort }
    if ($null -ne $frontendWorker -and $frontendWorker.Id -ne $frontendProcess.Id) {
        Add-ProcessToJob -Job $jobHandle -Process $frontendWorker
        $frontendWorker.PriorityClass = [Diagnostics.ProcessPriorityClass]::BelowNormal
    }

    if (-not $SmokeTest -and -not $NoBrowser) {
        $browserPath = Find-Browser
        if (-not $browserPath) { throw "Microsoft Edge or Google Chrome was not found." }
        Stop-StaleLauncherBrowser
        $browserArgs = @(
            "--app=$loadingUrl",
            "--window-size=1920,1080",
            "--user-data-dir=$browserProfile",
            "--new-window",
            "--no-first-run",
            "--disable-background-mode",
            "--disable-extensions"
        )
        $startedBrowserProcess = Start-Process -FilePath $browserPath -ArgumentList $browserArgs -PassThru
        $browserProcess = Resolve-LaunchedBrowserProcess -InitialProcess $startedBrowserProcess -TimeoutSeconds 8
        if ($null -eq $browserProcess) { throw "The startup screen did not open or could not be tracked." }
        Write-LauncherLog "Startup screen opened; loading backend services."
    }

    Start-ManagedHermesSidecar

    $backendArgs = @("-m", "uvicorn", "app:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "$BackendPort")
    $backendProcess = Start-Process -FilePath $pythonPath -ArgumentList $backendArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -PassThru
    Add-ProcessToJob -Job $jobHandle -Process $backendProcess
    $backendProcess.PriorityClass = [Diagnostics.ProcessPriorityClass]::BelowNormal

    $readyState = Wait-BackendReady -Url "$backendUrl/api/health" -ServiceProcess $backendProcess -WebsiteProcess $browserProcess -TimeoutSeconds 90
    if ($readyState -eq "window_closed") {
        Write-LauncherLog "Startup screen closed; stopping workbench services."
        exit 0
    }
    if ($readyState -eq "service_stopped") { throw "The backend stopped during startup. See $backendErr" }
    if ($readyState -ne "ready") { throw "The backend did not become ready within 90 seconds. See $backendErr" }
    if ($null -ne $backendProcess) { $backendWorker = Get-ServiceWorker -LauncherProcess $backendProcess -Port $BackendPort }
    if ($null -ne $backendWorker -and $backendWorker.Id -ne $backendProcess.Id) {
        Add-ProcessToJob -Job $jobHandle -Process $backendWorker
        $backendWorker.PriorityClass = [Diagnostics.ProcessPriorityClass]::BelowNormal
    }
    Write-LauncherLog "Workbench is ready; the startup screen will enter $websiteUrl"

    if ($SmokeTest) { Write-LauncherLog "Smoke test passed."; exit 0 }
    if ($NoBrowser) {
        while (($null -eq $backendWorker -or -not $backendWorker.HasExited) -and ($null -eq $frontendWorker -or -not $frontendWorker.HasExited)) {
            Invoke-HermesMonitorTick
            Start-Sleep -Seconds 2
        }
        exit 0
    }

    while (-not $browserProcess.HasExited) {
        if (($null -ne $backendWorker -and $backendWorker.HasExited) -or ($null -ne $frontendWorker -and $frontendWorker.HasExited)) { throw "A workbench service stopped unexpectedly." }
        Invoke-HermesMonitorTick
        Start-Sleep -Seconds 1
    }
    Write-LauncherLog "Website window closed; stopping workbench services."
}
catch {
    if (Test-Path -LiteralPath $logDir) { Write-LauncherLog "ERROR: $($_.Exception.Message)" }
    if ($null -ne $startupProgressScript -and (Test-Path -LiteralPath $startupProgressScript) -and (Test-Path -LiteralPath $pythonPath)) {
        & $pythonPath $startupProgressScript fail --message "$($_.Exception.Message)" 2>$null | Out-Null
    }
    if (-not $SmokeTest -and -not $NoBrowser) {
        Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
        [System.Windows.MessageBox]::Show("$($_.Exception.Message)`n`nDetails: $launcherLog", "Local AI Workbench", "OK", "Error") | Out-Null
    }
    exit 1
}
finally {
    Stop-OwnedProcess -Process $browserProcess
    Stop-OwnedProcess -Process $frontendWorker
    Stop-OwnedProcess -Process $backendWorker
    Stop-OwnedProcess -Process $frontendProcess
    Stop-OwnedProcess -Process $backendProcess
    Stop-HermesSidecar -Process $hermesProcess
    if ($hermesEnvironmentManaged) {
        [Environment]::SetEnvironmentVariable(
            "HERMES_API_SERVER_KEY",
            $previousHermesApiServerKey,
            "Process"
        )
    }
    if ($jobHandle -ne [IntPtr]::Zero) { [Workbench.NativeMethods]::CloseHandle($jobHandle) | Out-Null }
    if ($hasMutex -and $null -ne $mutex) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
