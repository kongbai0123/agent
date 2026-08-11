[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Check", "Apply")]
    [string]$Mode,
    [string]$RepositoryRoot = "",
    [string]$ExpectedRemoteUrl = "https://github.com/kongbai0123/agent",
    [ValidatePattern("^[A-Za-z0-9._-]+$")]
    [string]$RemoteName = "origin",
    [ValidatePattern("^[A-Za-z0-9._/-]+$")]
    [string]$Branch = "main",
    [switch]$OutputJson,
    [switch]$SkipValidation,
    [switch]$SkipRestart,
    [switch]$Restart,
    [switch]$ShowDialogs,
    [switch]$TestAllowCustomSource,
    [ValidateSet("None", "AfterMerge", "BeforeRestart")]
    [string]$TestFailurePoint = "None"
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
}
$RepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
$defaultRemoteUrl = "https://github.com/kongbai0123/agent"
$runtimeRoot = if ([string]::IsNullOrWhiteSpace($env:WORKBENCH_RUNTIME_DIR)) {
    Join-Path $RepositoryRoot "runtime"
}
else {
    [System.IO.Path]::GetFullPath($env:WORKBENCH_RUNTIME_DIR)
}
$runtimeUpdateDir = Join-Path $runtimeRoot "update"
$updateLog = Join-Path $runtimeUpdateDir "update.log"
$previousVersionPath = Join-Path $runtimeUpdateDir "previous-version.json"
$remoteRef = "refs/remotes/$RemoteName/$Branch"
$powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$script:GitPath = $null
$script:UpdateMutex = $null
$script:LauncherMutex = $null
$updateMutexName = "Local\LocalAIWorkbenchUpdater"
$launcherMutexName = "Local\LlmWorkbenchLauncher"
if ($TestAllowCustomSource) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $rootHash = $sha256.ComputeHash(
            [System.Text.Encoding]::UTF8.GetBytes($RepositoryRoot)
        )
        $launcherMutexName = "Local\LlmWorkbenchUpdaterTest-" + (
            [BitConverter]::ToString($rootHash, 0, 8).Replace("-", "")
        )
        $updateMutexName = "Local\LocalAIWorkbenchUpdaterTest-" + (
            [BitConverter]::ToString($rootHash, 8, 8).Replace("-", "")
        )
    }
    finally {
        $sha256.Dispose()
    }
}

function Write-UpdateLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force -Path $runtimeUpdateDir | Out-Null
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $updateLog -Value "[$timestamp] $Message" -Encoding UTF8
}

function Show-UpdateMessage {
    param(
        [string]$Message,
        [string]$Icon = "Information"
    )
    if (-not $ShowDialogs) { return }
    try {
        Add-Type -AssemblyName PresentationFramework -ErrorAction Stop
        [System.Windows.MessageBox]::Show(
            $Message,
            "Local AI Workbench Update",
            "OK",
            $Icon
        ) | Out-Null
    }
    catch { Write-UpdateLog "Unable to show update dialog: $($_.Exception.Message)" }
}

function New-UpdateResult {
    param(
        [string]$Status,
        [string]$Message,
        [string]$CurrentCommit = "",
        [string]$RemoteCommit = "",
        [int]$AheadBy = 0,
        [int]$BehindBy = 0,
        [string[]]$ChangedFiles = @()
    )
    return [PSCustomObject]@{
        status = $Status
        message = $Message
        current_commit = $CurrentCommit
        remote_commit = $RemoteCommit
        ahead_by = $AheadBy
        behind_by = $BehindBy
        changed_files = @($ChangedFiles)
        checked_at_utc = [DateTime]::UtcNow.ToString("o")
    }
}

function Write-Result {
    param([object]$Result)
    Write-UpdateLog "$($Result.status): $($Result.message)"
    if ($OutputJson) {
        Write-Output ($Result | ConvertTo-Json -Compress -Depth 4)
    }
}

function Quote-ProcessArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-ProcessCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $RepositoryRoot,
        [int]$TimeoutSeconds = 30,
        [hashtable]$Environment = @{}
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument -Value ([string]$_) }) -join " ")
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($name in $Environment.Keys) {
        $startInfo.EnvironmentVariables[[string]$name] = [string]$Environment[$name]
    }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Unable to start $FilePath" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        throw "Process timed out after $TimeoutSeconds seconds: $FilePath"
    }
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    return [PSCustomObject]@{
        ExitCode = $process.ExitCode
        Stdout = [string]$stdout
        Stderr = [string]$stderr
        Output = (([string]$stdout) + ([string]$stderr)).Trim()
    }
}

function Get-GitPath {
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    $fallback = Join-Path ${env:ProgramFiles} "Git\cmd\git.exe"
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    throw "Git for Windows is required for one-click updates."
}

function Invoke-Git {
    param(
        [string[]]$Arguments,
        [int]$TimeoutSeconds = 30
    )
    if (-not $script:GitPath) { $script:GitPath = Get-GitPath }
    return Invoke-ProcessCapture `
        -FilePath $script:GitPath `
        -Arguments (@("-C", $RepositoryRoot) + $Arguments) `
        -WorkingDirectory $RepositoryRoot `
        -TimeoutSeconds $TimeoutSeconds `
        -Environment @{
            GIT_TERMINAL_PROMPT = "0"
            GCM_INTERACTIVE = "Never"
        }
}

function Normalize-RemoteUrl {
    param([string]$Url)
    $value = $Url.Trim().TrimEnd("/")
    if ($value -match '^git@github\.com:(.+)$') {
        $value = "https://github.com/$($Matches[1])"
    }
    $value = $value -replace '\.git$', ''
    if ($value -match '^https://github\.com/') {
        return $value.ToLowerInvariant()
    }
    return $value
}

function Test-ProductionUpdateSource {
    if ($TestAllowCustomSource) { return $true }
    return (
        $RemoteName -eq "origin" -and
        $Branch -eq "main" -and
        (Normalize-RemoteUrl -Url $ExpectedRemoteUrl) -eq $defaultRemoteUrl
    )
}

function Get-CurrentBranch {
    $branch = Invoke-Git -Arguments @("symbolic-ref", "--quiet", "--short", "HEAD")
    if ($branch.ExitCode -ne 0) { return "" }
    return $branch.Stdout.Trim()
}

function Get-TrackedChanges {
    $status = Invoke-Git -Arguments @("status", "--porcelain", "--untracked-files=all")
    if ($status.ExitCode -ne 0) { throw "Unable to inspect the Git working tree: $($status.Output)" }
    return @($status.Stdout -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Test-LauncherMutexHeld {
    $probe = [Threading.Mutex]::new($false, $launcherMutexName)
    $acquired = $false
    try {
        $acquired = $probe.WaitOne(0, $false)
        return -not $acquired
    }
    catch [Threading.AbandonedMutexException] {
        $acquired = $true
        return $false
    }
    finally {
        if ($acquired) {
            try { $probe.ReleaseMutex() } catch { }
        }
        $probe.Dispose()
    }
}

function Get-RunningWorkbenchProcess {
    $escapedRoot = [Regex]::Escape($RepositoryRoot)
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            (
                (
                    $_.Name -in @("powershell.exe", "pwsh.exe") -and
                    [string]$_.CommandLine -match (
                        "(?i)$escapedRoot.+scripts[\\/](?:launch|start)_workbench\.ps1"
                    )
                ) -or
                (
                    $_.Name -match "^(?:python|pythonw)\.exe$" -and
                    [string]$_.CommandLine -match "(?i)$escapedRoot.+(?:uvicorn|startup_http_server)"
                )
            )
        } |
        Select-Object -First 1
}

function Test-WorkbenchRunning {
    if (Test-LauncherMutexHeld) { return $true }
    return $null -ne (Get-RunningWorkbenchProcess)
}

function Acquire-LauncherMaintenanceMutex {
    $script:LauncherMutex = [Threading.Mutex]::new(
        $false,
        $launcherMutexName
    )
    try {
        if (-not $script:LauncherMutex.WaitOne(0, $false)) {
            throw "Local AI Workbench started while the update was being validated."
        }
    }
    catch [Threading.AbandonedMutexException] {
        # An abandoned mutex is now owned by this updater and is safe to use.
    }
}

function Release-LauncherMaintenanceMutex {
    if ($null -eq $script:LauncherMutex) { return }
    try { $script:LauncherMutex.ReleaseMutex() } catch { }
    $script:LauncherMutex.Dispose()
    $script:LauncherMutex = $null
}

function Get-RequiredCiState {
    param([string]$Commit)
    if ($TestAllowCustomSource) {
        return [PSCustomObject]@{ success = $true; message = "test source" }
    }
    try {
        $headers = @{
            Accept = "application/vnd.github+json"
            "User-Agent" = "LocalAIWorkbench-Updater"
            "X-GitHub-Api-Version" = "2022-11-28"
        }
        $url = "https://api.github.com/repos/kongbai0123/agent/commits/$Commit/check-runs?per_page=100"
        $response = Invoke-RestMethod `
            -Uri $url `
            -Headers $headers `
            -Method Get `
            -TimeoutSec 15
        $required = @(
            $response.check_runs |
                Where-Object { $_.name -eq "deterministic-tests" } |
                Sort-Object -Property started_at -Descending
        ) | Select-Object -First 1
        if ($null -eq $required) {
            return [PSCustomObject]@{
                success = $false
                message = "The required deterministic GitHub CI result is not available yet."
            }
        }
        if ($required.status -ne "completed" -or $required.conclusion -ne "success") {
            return [PSCustomObject]@{
                success = $false
                message = "The required deterministic GitHub CI check has not completed successfully."
            }
        }
        return [PSCustomObject]@{
            success = $true
            message = "Required deterministic GitHub CI succeeded."
        }
    }
    catch {
        return [PSCustomObject]@{
            success = $false
            message = "GitHub CI status could not be verified; the installed version remains unchanged."
        }
    }
}

function Get-UpdateState {
    if (-not (Test-Path -LiteralPath (Join-Path $RepositoryRoot ".git"))) {
        return New-UpdateResult -Status "unavailable" -Message "This installation is not a Git checkout."
    }
    if (-not (Test-ProductionUpdateSource)) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Production updates are restricted to the built-in origin/main GitHub source."
    }
    if (-not $TestAllowCustomSource -and $TestFailurePoint -ne "None") {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Failure injection is available only for isolated updater tests."
    }

    $remoteUrlResult = Invoke-Git -Arguments @("remote", "get-url", $RemoteName)
    if ($remoteUrlResult.ExitCode -ne 0) {
        return New-UpdateResult -Status "unavailable" -Message "Git remote '$RemoteName' is not configured."
    }
    $actualRemote = Normalize-RemoteUrl -Url $remoteUrlResult.Stdout
    $expectedRemote = Normalize-RemoteUrl -Url $ExpectedRemoteUrl
    if ($actualRemote -ne $expectedRemote) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Update source mismatch. Expected $expectedRemote but found $actualRemote."
    }
    if (
        -not $TestAllowCustomSource -and
        $remoteUrlResult.Stdout.Trim() -notmatch (
            "^https://github\.com/kongbai0123/agent(?:\.git)?/?$"
        )
    ) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Production updates require the fixed HTTPS GitHub origin."
    }

    $currentBranch = Get-CurrentBranch
    if ($currentBranch -ne $Branch) {
        $displayBranch = if ($currentBranch) { $currentBranch } else { "detached HEAD" }
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Automatic updates require the '$Branch' branch; current checkout is '$displayBranch'."
    }

    $fetch = Invoke-Git `
        -Arguments @(
            "-c", "http.lowSpeedLimit=1000",
            "-c", "http.lowSpeedTime=5",
            "fetch", "--quiet", "--no-tags", $RemoteName,
            "+refs/heads/$Branch`:$remoteRef"
        ) `
        -TimeoutSeconds 25
    if ($fetch.ExitCode -ne 0) {
        return New-UpdateResult -Status "unavailable" -Message "GitHub update check failed; the installed version can still run."
    }

    $currentResult = Invoke-Git -Arguments @("rev-parse", "HEAD")
    $remoteResult = Invoke-Git -Arguments @("rev-parse", $remoteRef)
    if ($currentResult.ExitCode -ne 0 -or $remoteResult.ExitCode -ne 0) {
        return New-UpdateResult -Status "unavailable" -Message "Unable to resolve local or remote commit identity."
    }
    $currentCommit = $currentResult.Stdout.Trim()
    $remoteCommit = $remoteResult.Stdout.Trim()

    $countsResult = Invoke-Git -Arguments @("rev-list", "--left-right", "--count", "HEAD...$remoteRef")
    if ($countsResult.ExitCode -ne 0) {
        return New-UpdateResult `
            -Status "unavailable" `
            -Message "Unable to compare the installed version with GitHub." `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit
    }
    $parts = @($countsResult.Stdout.Trim() -split '\s+')
    $aheadBy = if ($parts.Count -ge 1) { [int]$parts[0] } else { 0 }
    $behindBy = if ($parts.Count -ge 2) { [int]$parts[1] } else { 0 }

    if ($behindBy -eq 0) {
        $message = if ($aheadBy -gt 0) {
            "The local checkout contains commits that are not on GitHub main; no automatic update was applied."
        } else {
            "Local AI Workbench is up to date."
        }
        return New-UpdateResult `
            -Status "current" `
            -Message $message `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -AheadBy $aheadBy
    }

    $ciState = Get-RequiredCiState -Commit $remoteCommit
    if (-not $ciState.success) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message $ciState.message `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -AheadBy $aheadBy `
            -BehindBy $behindBy
    }

    $diffResult = Invoke-Git -Arguments @("diff", "--name-only", "HEAD", $remoteRef)
    if ($diffResult.ExitCode -ne 0) {
        return New-UpdateResult `
            -Status "unavailable" `
            -Message "Unable to enumerate update contents." `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -AheadBy $aheadBy `
            -BehindBy $behindBy
    }
    $changedFiles = @(
        $diffResult.Stdout -split "`r?`n" |
            ForEach-Object { $_.Trim().Replace("\", "/") } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )

    if ($aheadBy -gt 0) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "The local and GitHub histories have diverged; automatic update requires a fast-forward history." `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -AheadBy $aheadBy `
            -BehindBy $behindBy `
            -ChangedFiles $changedFiles
    }

    $trackedChanges = Get-TrackedChanges
    if ($trackedChanges.Count -gt 0) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Tracked project files contain local changes. Commit or restore them before updating; the updater will not stash or overwrite them." `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -BehindBy $behindBy `
            -ChangedFiles $changedFiles
    }

    $protectedUpdatePaths = @(
        "backend/requirements.txt",
        "backend/requirements.lock",
        "backend/paths.py",
        "backend/secret_store.py",
        "backend/database.py",
        ".gitignore",
        ".venv/",
        ".env",
        ".env.",
        "runtime/",
        "workspaces/",
        "projects/",
        "artifacts/",
        "archive/",
        "data/",
        "backend/data/",
        "backend/settings.json"
    )
    $requiresFullUpdate = @(
        $changedFiles | Where-Object {
            $candidate = $_
            $protectedUpdatePaths | Where-Object {
                $candidate -eq $_ -or
                $candidate.StartsWith($_, [StringComparison]::OrdinalIgnoreCase)
            }
        }
    )
    if ($requiresFullUpdate.Count -gt 0) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "This update changes runtime, dependency, or user-data boundaries and requires a full packaged update." `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -BehindBy $behindBy `
            -ChangedFiles $changedFiles
    }

    if (Test-WorkbenchRunning) {
        return New-UpdateResult `
            -Status "blocked" `
            -Message "Close the currently running Workbench window before applying this update." `
            -CurrentCommit $currentCommit `
            -RemoteCommit $remoteCommit `
            -BehindBy $behindBy `
            -ChangedFiles $changedFiles
    }

    return New-UpdateResult `
        -Status "available" `
        -Message "A validated fast-forward update is available." `
        -CurrentCommit $currentCommit `
        -RemoteCommit $remoteCommit `
        -BehindBy $behindBy `
        -ChangedFiles $changedFiles
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Assert-PowerShellSyntax {
    param([string]$Root)
    $parseErrors = @()
    Get-ChildItem -LiteralPath (Join-Path $Root "scripts") -Filter "*.ps1" -File |
        ForEach-Object {
            $tokens = $null
            $errors = $null
            [System.Management.Automation.Language.Parser]::ParseFile(
                $_.FullName,
                [ref]$tokens,
                [ref]$errors
            ) | Out-Null
            if ($errors.Count -gt 0) {
                $parseErrors += $errors | ForEach-Object { "$($_.Extent.File): $($_.Message)" }
            }
        }
    if ($parseErrors.Count -gt 0) {
        throw "PowerShell validation failed: $($parseErrors -join '; ')"
    }
}

function Invoke-StagedValidation {
    param([string]$Commit)
    if ($SkipValidation) {
        Write-UpdateLog "Staged validation skipped by explicit command-line option."
        return
    }

    $pythonPath = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "The project Python environment is missing; the update cannot be validated."
    }

    $tempBase = [System.IO.Path]::GetFullPath(
        (Join-Path ([System.IO.Path]::GetTempPath()) "LocalAIWorkbenchUpdates")
    )
    New-Item -ItemType Directory -Force -Path $tempBase | Out-Null
    $stagingRoot = Join-Path $tempBase ("stage-" + [Guid]::NewGuid().ToString("N"))
    $venvJunction = Join-Path $stagingRoot ".venv"
    $worktreeAdded = $false

    try {
        $worktree = Invoke-Git `
            -Arguments @("worktree", "add", "--detach", "--force", $stagingRoot, $Commit) `
            -TimeoutSeconds 60
        if ($worktree.ExitCode -ne 0) {
            throw "Unable to create the isolated update staging area: $($worktree.Output)"
        }
        $worktreeAdded = $true
        New-Item -ItemType Junction -Path $venvJunction -Target (Split-Path -Parent (Split-Path -Parent $pythonPath)) | Out-Null

        Assert-PowerShellSyntax -Root $stagingRoot

        $compile = Invoke-ProcessCapture `
            -FilePath $pythonPath `
            -Arguments @("-m", "compileall", "-q", "backend", "scripts") `
            -WorkingDirectory $stagingRoot `
            -TimeoutSeconds 90
        if ($compile.ExitCode -ne 0) {
            throw "Python compile validation failed: $($compile.Output)"
        }

        $tests = Invoke-ProcessCapture `
            -FilePath $pythonPath `
            -Arguments @(
                "-m", "pytest",
                "tests/test_startup_loading.py",
                "tests/test_app_icon.py",
                "tests/test_api_security.py",
                "tests/test_windows_launcher.py",
                "tests/test_workbench_updater.py",
                "-q"
            ) `
            -WorkingDirectory $stagingRoot `
            -TimeoutSeconds 120
        if ($tests.ExitCode -ne 0) {
            throw "Update contract tests failed: $($tests.Output)"
        }

        $backendPort = Get-FreeTcpPort
        $frontendPort = Get-FreeTcpPort
        while ($frontendPort -eq $backendPort) { $frontendPort = Get-FreeTcpPort }
        $stagedLauncher = Join-Path $stagingRoot "LocalAIWorkbench.exe"
        if (-not (Test-Path -LiteralPath $stagedLauncher)) {
            throw "The staged Windows launcher is missing."
        }
        $smoke = Invoke-ProcessCapture `
            -FilePath $stagedLauncher `
            -Arguments @(
                "--skip-update",
                "--smoke-test",
                "--no-browser",
                "--wait",
                "--backend-port", "$backendPort",
                "--frontend-port", "$frontendPort"
            ) `
            -WorkingDirectory $stagingRoot `
            -TimeoutSeconds 150
        if ($smoke.ExitCode -ne 0) {
            throw "Update startup smoke test failed: $($smoke.Output)"
        }
        Write-UpdateLog "Isolated validation passed for $Commit."
    }
    finally {
        if (Test-Path -LiteralPath $venvJunction) {
            [System.IO.DirectoryInfo]::new($venvJunction).Delete()
        }
        if ($worktreeAdded) {
            $remove = Invoke-Git `
                -Arguments @("worktree", "remove", "--force", $stagingRoot) `
                -TimeoutSeconds 60
            if ($remove.ExitCode -ne 0) {
                Write-UpdateLog "Staging worktree cleanup warning: $($remove.Output)"
            }
            Invoke-Git -Arguments @("worktree", "prune") | Out-Null
        }
    }
}

function Save-PreviousVersion {
    param(
        [string]$PreviousCommit,
        [string]$InstalledCommit,
        [string]$Status = "installed"
    )
    New-Item -ItemType Directory -Force -Path $runtimeUpdateDir | Out-Null
    $payload = [PSCustomObject]@{
        previous_commit = $PreviousCommit
        installed_commit = $InstalledCommit
        status = $Status
        installed_at_utc = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json -Depth 3
    $temporaryPath = "$previousVersionPath.tmp"
    [System.IO.File]::WriteAllText($temporaryPath, $payload, [System.Text.Encoding]::UTF8)
    Move-Item -LiteralPath $temporaryPath -Destination $previousVersionPath -Force
}

function Assert-ApplyPreconditions {
    param([object]$State)
    $current = Invoke-Git -Arguments @("rev-parse", "HEAD")
    if ($current.ExitCode -ne 0 -or $current.Stdout.Trim() -ne $State.current_commit) {
        throw "The installed commit changed while the update was being validated."
    }
    if ((Get-CurrentBranch) -ne $Branch) {
        throw "The checked-out branch changed while the update was being validated."
    }
    $changes = Get-TrackedChanges
    if ($changes.Count -gt 0) {
        throw "Project files changed while the update was being validated; no update was applied."
    }
}

function Backup-UserDatabase {
    $databaseDir = Join-Path $runtimeRoot "db"
    $databaseNames = @("workbench.db", "workbench.db-wal", "workbench.db-shm")
    $existing = @(
        $databaseNames | Where-Object {
            Test-Path -LiteralPath (Join-Path $databaseDir $_)
        }
    )
    if ($existing.Count -eq 0) {
        return [PSCustomObject]@{
            backup_dir = ""
            existing_names = @()
        }
    }
    $backupDir = Join-Path $runtimeUpdateDir (
        "db-backup-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmssfff")
    )
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    foreach ($name in $existing) {
        Copy-Item `
            -LiteralPath (Join-Path $databaseDir $name) `
            -Destination (Join-Path $backupDir $name) `
            -Force
    }
    return [PSCustomObject]@{
        backup_dir = $backupDir
        existing_names = @($existing)
    }
}

function Restore-UserDatabase {
    param([object]$Backup)
    if ($null -eq $Backup) { return }
    $databaseDir = [System.IO.Path]::GetFullPath((Join-Path $runtimeRoot "db"))
    New-Item -ItemType Directory -Force -Path $databaseDir | Out-Null
    $databaseNames = @("workbench.db", "workbench.db-wal", "workbench.db-shm")
    foreach ($name in $databaseNames) {
        $target = [System.IO.Path]::GetFullPath((Join-Path $databaseDir $name))
        if (-not $target.StartsWith($databaseDir, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to restore a database path outside the runtime database directory."
        }
        if ($Backup.existing_names -contains $name) {
            Copy-Item `
                -LiteralPath (Join-Path $Backup.backup_dir $name) `
                -Destination $target `
                -Force
        }
        elseif (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
        }
    }
}

function Restore-PreviousCommit {
    param(
        [string]$PreviousCommit,
        [string]$FailedCommit,
        [object]$DatabaseBackup
    )
    $current = Invoke-Git -Arguments @("rev-parse", "HEAD")
    if ($current.ExitCode -ne 0 -or $current.Stdout.Trim() -ne $FailedCommit) {
        throw "Automatic rollback refused because HEAD no longer matches the failed update."
    }
    $changes = Get-TrackedChanges
    if ($changes.Count -gt 0) {
        throw "Automatic rollback refused because project files changed after the update."
    }
    $rollback = Invoke-Git `
        -Arguments @("reset", "--keep", $PreviousCommit) `
        -TimeoutSeconds 90
    if ($rollback.ExitCode -ne 0) {
        throw "Automatic code rollback failed: $($rollback.Output)"
    }
    Restore-UserDatabase -Backup $DatabaseBackup
    try {
        Save-PreviousVersion `
            -PreviousCommit $FailedCommit `
            -InstalledCommit $PreviousCommit `
            -Status "rolled_back"
    }
    catch {
        Write-UpdateLog "Rollback metadata warning: $($_.Exception.Message)"
    }
    Write-UpdateLog "Rolled back failed update $FailedCommit to $PreviousCommit."
}

function Start-UpdatedWorkbench {
    if ($SkipRestart -or -not $Restart) { return }
    $launchScript = Join-Path $RepositoryRoot "scripts\launch_workbench.ps1"
    if (-not (Test-Path -LiteralPath $launchScript)) {
        throw "The updated bootstrap launcher is missing at $launchScript"
    }
    $backendPort = Get-FreeTcpPort
    $frontendPort = Get-FreeTcpPort
    while ($frontendPort -eq $backendPort) { $frontendPort = Get-FreeTcpPort }
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $powershellPath
    $startInfo.Arguments = (
        @(
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-File", $launchScript,
            "-SkipUpdate",
            "-UpdateResume",
            "-BackendPort", "$backendPort",
            "-FrontendPort", "$frontendPort"
        ) |
            ForEach-Object { Quote-ProcessArgument -Value ([string]$_) }
    ) -join " "
    $startInfo.WorkingDirectory = $RepositoryRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Windows could not restart Local AI Workbench."
    }

    $healthUrl = "http://127.0.0.1:$backendPort/api/health"
    $deadline = (Get-Date).AddSeconds(120)
    do {
        if ($process.HasExited) {
            throw "The updated Workbench exited before its health check succeeded."
        }
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
            if ($health.success -eq $true) {
                Write-UpdateLog "Updated Workbench health check passed at $healthUrl."
                return
            }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    try {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit(5000) | Out-Null
        }
    }
    catch { }
    throw "The updated Workbench did not become healthy within 120 seconds."
}

function Invoke-UpdateApply {
    $script:UpdateMutex = [Threading.Mutex]::new($false, $updateMutexName)
    if (-not $script:UpdateMutex.WaitOne(0, $false)) {
        return New-UpdateResult -Status "blocked" -Message "Another Workbench update is already running."
    }

    $state = Get-UpdateState
    if ($state.status -eq "current") {
        Start-UpdatedWorkbench
        return $state
    }
    if ($state.status -ne "available") { return $state }

    Invoke-StagedValidation -Commit $state.remote_commit
    Acquire-LauncherMaintenanceMutex
    Assert-ApplyPreconditions -State $state
    $databaseBackup = Backup-UserDatabase
    $mergeApplied = $false
    try {
        $merge = Invoke-Git `
            -Arguments @("merge", "--ff-only", $state.remote_commit) `
            -TimeoutSeconds 90
        if ($merge.ExitCode -ne 0) {
            throw "Fast-forward update failed without overwriting local changes: $($merge.Output)"
        }
        $mergeApplied = $true
        $installed = Invoke-Git -Arguments @("rev-parse", "HEAD")
        if ($installed.ExitCode -ne 0 -or $installed.Stdout.Trim() -ne $state.remote_commit) {
            throw "The installed commit does not match the validated GitHub commit."
        }
        if ($TestFailurePoint -eq "AfterMerge") {
            throw "Injected updater failure after merge."
        }

        Save-PreviousVersion `
            -PreviousCommit $state.current_commit `
            -InstalledCommit $state.remote_commit
        if ($TestFailurePoint -eq "BeforeRestart") {
            throw "Injected updater failure before restart."
        }
        Write-UpdateLog "Fast-forward update applied: $($state.current_commit) -> $($state.remote_commit)."

        Release-LauncherMaintenanceMutex
        Start-UpdatedWorkbench
        return New-UpdateResult `
            -Status "applied" `
            -Message "Update installed and validated successfully." `
            -CurrentCommit $state.remote_commit `
            -RemoteCommit $state.remote_commit `
            -ChangedFiles $state.changed_files
    }
    catch {
        $updateFailure = $_
        Release-LauncherMaintenanceMutex
        if ($mergeApplied) {
            Restore-PreviousCommit `
                -PreviousCommit $state.current_commit `
                -FailedCommit $state.remote_commit `
                -DatabaseBackup $databaseBackup
        }
        throw $updateFailure
    }
    finally {
        Release-LauncherMaintenanceMutex
    }
}

$result = $null
$exitCode = 0
try {
    if (-not (Test-Path -LiteralPath $RepositoryRoot)) {
        throw "Repository root does not exist: $RepositoryRoot"
    }
    if ($Mode -eq "Check") {
        $result = Get-UpdateState
    }
    else {
        $result = Invoke-UpdateApply
    }

    $exitCode = switch ($result.status) {
        "current" { 0 }
        "applied" { 0 }
        "available" { 10 }
        "blocked" { 20 }
        "unavailable" { 30 }
        default { 40 }
    }
}
catch {
    try {
        Write-UpdateLog "Failure detail: $($_.Exception.ToString())"
        if ($_.ScriptStackTrace) {
            Write-UpdateLog "Failure stack: $($_.ScriptStackTrace)"
        }
    }
    catch { }
    $result = New-UpdateResult -Status "failed" -Message $_.Exception.Message
    $exitCode = 40
    Show-UpdateMessage `
        -Icon "Error" `
        -Message "The update failed. Existing project files were not reset automatically.`n`n$($_.Exception.Message)`n`nLog: $updateLog"
}
finally {
    Release-LauncherMaintenanceMutex
    if ($null -ne $script:UpdateMutex) {
        try { $script:UpdateMutex.ReleaseMutex() } catch { }
        $script:UpdateMutex.Dispose()
    }
}

Write-Result -Result $result
exit $exitCode
