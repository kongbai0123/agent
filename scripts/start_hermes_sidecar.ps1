[CmdletBinding()]
param(
    [ValidateSet("Native", "Docker")]
    [string]$DeploymentMode = "Native",

    [string]$RuntimeRoot = "",
    [string]$ManifestPath = "",
    [ValidateSet("NoTools", "ProjectReadOnly")]
    [string]$ToolPolicyProfile = "NoTools",
    [string]$ProjectRoot = "",
    [string]$ProjectId = "",
    [ValidateRange(5, 300)]
    [int]$StartupTimeoutSeconds = 60,
    [switch]$PassThru,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ExpectedRuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "runtime\hermes"))
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = $ExpectedRuntimeRoot
} else {
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
}
if (-not $RuntimeRoot.Equals($ExpectedRuntimeRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Hermes runtime root is fixed at $ExpectedRuntimeRoot; refusing target $RuntimeRoot"
}
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $RepoRoot "config\hermes-sidecar-manifest.json"
}
$ManifestPath = [System.IO.Path]::GetFullPath($ManifestPath)

function Get-LowerSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

function Get-LowerTextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Assert-ManifestAndPolicy {
    param([Parameter(Mandatory = $true)]$Manifest)

    if ($Manifest.release.package_version -ne "0.18.2" -or
        $Manifest.release.tag -ne "v2026.7.7.2" -or
        $Manifest.release.source_commit -ne "9de9c25f620ff7f1ce0fd5457d596052d5159596") {
        throw "Hermes release pin changed unexpectedly"
    }
    if ([string]$Manifest.runtime.host -ne "127.0.0.1" -or [int]$Manifest.runtime.host_port -ne 8642) {
        throw "Hermes sidecar may only bind to 127.0.0.1:8642"
    }
    if ([string]$Manifest.runtime.container_host -ne "0.0.0.0") {
        throw "Container-side API host must be 0.0.0.0 behind loopback-only publishing"
    }
    if ([string]$Manifest.model.base_urls.native -ne "http://127.0.0.1:11434/v1" -or
        [string]$Manifest.model.base_urls.docker -ne "http://host.docker.internal:11434/v1") {
        throw "Hermes mode-specific local-model endpoint pins changed unexpectedly"
    }
    if ([int]$Manifest.model.context_length -ne 64000 -or
        [int]$Manifest.model.max_output_tokens -ne 4096) {
        throw "Hermes context or output-reserve contract changed unexpectedly"
    }
    if (@($Manifest.initial_policy.platform_toolsets.api_server).Count -ne 0 -or
        @($Manifest.initial_policy.mcp_servers.psobject.Properties).Count -ne 0 -or
        @($Manifest.initial_policy.plugins.enabled).Count -ne 0 -or
        @($Manifest.initial_policy.disabled_toolsets).Count -lt 25) {
        throw "Hermes initial no-tool policy is incomplete"
    }
    if (-not [bool]$Manifest.security.requires_os_isolation_before_tools -or
        [bool]$Manifest.security.app_approval_is_os_boundary) {
        throw "Hermes OS-isolation security invariant changed unexpectedly"
    }
    $readonly = $Manifest.readonly_tool_policy
    if ([string]$readonly.profile -ne "project-readonly-v1" -or
        [string]$readonly.toolset -ne "workbench-readonly" -or
        (@($readonly.tools) -join ",") -ne "project_read_file,project_search_files" -or
        [bool]$readonly.writes_enabled -or [bool]$readonly.shell_enabled -or
        [bool]$readonly.network_tools_enabled) {
        throw "Hermes reviewed read-only tool policy changed unexpectedly"
    }
    foreach ($artifact in @($readonly.config_template, $readonly.python_policy)) {
        $artifactPath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ([string]$artifact.path)))
        if (-not $artifactPath.StartsWith($RepoRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $artifactPath -PathType Leaf) -or
            (Get-LowerSha256 -Path $artifactPath) -ne [string]$artifact.sha256) {
            throw "Hermes reviewed read-only policy artifact failed verification"
        }
    }
    $production = $Manifest.production_policy
    $readiness = $production.readiness
    $monitoring = $production.monitoring
    $rollback = $production.rollback
    $evidence = $production.evidence
    if ([int]$readiness.startup_timeout_seconds -ne 60 -or
        [int]$readiness.probe_interval_milliseconds -ne 500 -or
        [int]$readiness.required_consecutive_successes -ne 2 -or
        [string]$readiness.health.status -ne "ok" -or
        [string]$readiness.health.platform -ne "hermes-agent" -or
        [string]$readiness.health.version -ne "0.18.2" -or
        (@($readiness.required_features) -join ",") -ne "run_approval_response,run_events_sse,run_status,run_stop,run_submission" -or
        [string]$readiness.required_endpoints.runs -ne "/v1/runs" -or
        [string]$readiness.required_endpoints.run_status -ne "/v1/runs/{run_id}" -or
        [string]$readiness.required_endpoints.run_events -ne "/v1/runs/{run_id}/events" -or
        [string]$readiness.required_endpoints.run_approval -ne "/v1/runs/{run_id}/approval" -or
        [string]$readiness.required_endpoints.run_stop -ne "/v1/runs/{run_id}/stop" -or
        [int]$monitoring.probe_interval_seconds -ne 10 -or
        [int]$monitoring.failure_threshold -ne 3 -or
        [int]$monitoring.max_restarts_per_launch -ne 2 -or
        [int]$monitoring.restart_backoff_seconds -ne 2 -or
        [string]$rollback.safe_tool_policy_profile -ne "NoTools" -or
        $rollback.stop_owned_container_only -isnot [bool] -or -not [bool]$rollback.stop_owned_container_only -or
        $rollback.preserve_runtime_data -isnot [bool] -or -not [bool]$rollback.preserve_runtime_data -or
        [int]$evidence.schema_version -ne 1 -or
        [string]$evidence.relative_directory -ne "runtime/hermes/evidence" -or
        (@($evidence.forbidden_fields) -join ",") -ne "api_key,authorization,canary_session_ids,environment,project_id,project_root") {
        throw "Hermes production readiness and rollback policy changed unexpectedly"
    }
}

function Resolve-ReviewedProjectRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.IndexOf(',') -ge 0 -or $Path -match '[\x00-\x1f\x7f]') {
        throw "Hermes read-only project path is invalid"
    }
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $projectsRoot = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot "projects"))
    if ($resolved.Equals($projectsRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not $resolved.StartsWith($projectsRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Hermes read-only project must be one existing child of $projectsRoot"
    }
    $rootItem = Get-Item -LiteralPath $resolved -Force
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Hermes read-only project root may not be a link or junction"
    }
    $reparse = Get-ChildItem -LiteralPath $resolved -Force -Recurse -ErrorAction Stop |
        Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
        Select-Object -First 1
    if ($null -ne $reparse) {
        throw "Hermes read-only project contains a link or junction; refusing the mount"
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $resolved -Force -Recurse -File -ErrorAction Stop)) {
        $hardlinks = @(& fsutil.exe hardlink list $file.FullName 2>&1 |
            Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
        if ($LASTEXITCODE -ne 0 -or $hardlinks.Count -ne 1) {
            throw "Hermes read-only project contains a hard-linked file; refusing the mount"
        }
    }
    return $resolved
}

function Protect-SecretFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($env:OS -ne "Windows_NT") {
        return
    }
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $grant = "*$($sid):(F)"
    $null = & icacls.exe $Path /inheritance:r /grant:r $grant 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict ACL on temporary Hermes environment file"
    }
}

function Test-PortOpen {
    param([string]$HostName, [int]$Port)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(300)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Invoke-HermesEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ApiKey
    )

    $headers = @{ Authorization = "Bearer $ApiKey" }
    return Invoke-RestMethod -Method Get -Uri ($BaseUrl + $Path) -Headers $headers -TimeoutSec 5
}

function Assert-HermesToolPolicy {
    param(
        [Parameter(Mandatory = $true)]$ToolsetsResponse,
        [Parameter(Mandatory = $true)][ValidateSet("NoTools", "ProjectReadOnly")][string]$Profile
    )

    if ([string]$ToolsetsResponse.object -ne "list" -or
        [string]$ToolsetsResponse.platform -ne "api_server" -or
        $null -eq $ToolsetsResponse.data) {
        throw "Hermes /v1/toolsets returned an unknown shape; stopping fail closed"
    }
    $enabledNames = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @($ToolsetsResponse.data)) {
        $propertyNames = @($entry.psobject.Properties.Name)
        if ($propertyNames -notcontains "name" -or
            $propertyNames -notcontains "enabled" -or
            $propertyNames -notcontains "tools" -or
            $entry.enabled -isnot [bool]) {
            throw "Hermes /v1/toolsets returned an unknown entry shape; stopping fail closed"
        }
        if ([bool]$entry.enabled) {
            $enabledNames.Add([string]$entry.name)
        }
    }
    if ($Profile -eq "NoTools") {
        if ($enabledNames.Count -ne 0) {
            throw "Hermes exposed enabled toolsets despite the no-tool policy: $($enabledNames -join ', ')"
        }
        return
    }

    if ($enabledNames.Count -ne 1 -or $enabledNames[0] -ne "workbench-readonly") {
        throw "Hermes exposed a toolset outside the reviewed project read-only policy"
    }
    $readonlyEntry = @($ToolsetsResponse.data | Where-Object { [string]$_.name -eq "workbench-readonly" })
    if ($readonlyEntry.Count -ne 1 -or -not [bool]$readonlyEntry[0].enabled) {
        throw "Hermes did not expose the reviewed Workbench read-only toolset"
    }
    $actualTools = @($readonlyEntry[0].tools | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    if (($actualTools -join ",") -ne "project_read_file,project_search_files") {
        throw "Hermes read-only toolset contains an unexpected tool"
    }
}

function Assert-HermesHealth {
    param(
        [Parameter(Mandatory = $true)]$HealthResponse,
        [Parameter(Mandatory = $true)]$ReadinessPolicy
    )

    if ([string]$HealthResponse.status -ne [string]$ReadinessPolicy.health.status -or
        [string]$HealthResponse.platform -ne [string]$ReadinessPolicy.health.platform -or
        [string]$HealthResponse.version -ne [string]$ReadinessPolicy.health.version) {
        throw "Hermes health response did not satisfy the reviewed readiness contract"
    }
}

function Assert-HermesCapabilities {
    param(
        [Parameter(Mandatory = $true)]$CapabilitiesResponse,
        [Parameter(Mandatory = $true)]$ReadinessPolicy
    )

    if ([string]$CapabilitiesResponse.object -ne "hermes.api_server.capabilities" -or
        [string]$CapabilitiesResponse.platform -ne "hermes-agent" -or
        [string]$CapabilitiesResponse.auth.type -ne "bearer" -or
        $CapabilitiesResponse.auth.required -isnot [bool] -or
        -not [bool]$CapabilitiesResponse.auth.required -or
        [string]$CapabilitiesResponse.runtime.mode -ne "server_agent" -or
        [string]$CapabilitiesResponse.runtime.tool_execution -ne "server" -or
        $CapabilitiesResponse.runtime.split_runtime -isnot [bool] -or
        [bool]$CapabilitiesResponse.runtime.split_runtime) {
        throw "Hermes capabilities response changed its reviewed runtime boundary"
    }
    foreach ($feature in @($ReadinessPolicy.required_features)) {
        $property = $CapabilitiesResponse.features.PSObject.Properties[[string]$feature]
        if ($null -eq $property -or $property.Value -isnot [bool] -or -not [bool]$property.Value) {
            throw "Hermes Runs capabilities are incomplete"
        }
    }
    $expectedMethods = @{
        runs = "POST"
        run_status = "GET"
        run_events = "GET"
        run_approval = "POST"
        run_stop = "POST"
    }
    foreach ($endpointName in $expectedMethods.Keys) {
        $endpoint = $CapabilitiesResponse.endpoints.PSObject.Properties[$endpointName]
        $expectedPath = $ReadinessPolicy.required_endpoints.PSObject.Properties[$endpointName]
        if ($null -eq $endpoint -or $null -eq $expectedPath -or
            [string]$endpoint.Value.method -ne [string]$expectedMethods[$endpointName] -or
            [string]$endpoint.Value.path -ne [string]$expectedPath.Value) {
            throw "Hermes Runs endpoint contract is incomplete"
        }
    }
}

function Stop-StartedDockerSidecar {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][ValidateSet("NoTools", "ProjectReadOnly")][string]$Profile
    )

    if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) { return }
    $containerName = [string]$Manifest.runtime.container_name
    if ($containerName -ne "local-ai-workbench-hermes") { return }
    $expectedPolicy = if ($Profile -eq "ProjectReadOnly") { "project-readonly-v1" } else { "no-tools-v1" }
    try {
        $containerIdOutput = @(& docker.exe container inspect --format "{{.Id}}" $containerName 2>&1)
        if ($LASTEXITCODE -ne 0) { return }
        $containerId = [string]($containerIdOutput | Select-Object -First 1)
        if ($containerId -notmatch '^[0-9a-f]{64}$') { return }
        $labelsOutput = @(& docker.exe container inspect --format "{{json .Config.Labels}}" $containerId 2>&1)
        if ($LASTEXITCODE -ne 0) { return }
        $labels = (($labelsOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop)
        if ([string]$labels.'com.local-ai-workbench.owner' -ne "workbench" -or
            [string]$labels.'com.local-ai-workbench.component' -ne "hermes-sidecar" -or
            [string]$labels.'com.local-ai-workbench.policy' -ne $expectedPolicy) {
            return
        }
        $null = & docker.exe container stop --time 10 $containerId 2>&1
        $remaining = @(& docker.exe container ls --all --filter "id=$containerId" --format "{{.ID}}" 2>&1)
        if ($LASTEXITCODE -eq 0 -and
            @($remaining | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }).Count -ne 0) {
            $null = & docker.exe container rm $containerId 2>&1
        }
    }
    catch {
        # Startup is already failing. Never replace its reviewed error with raw
        # Docker output, and never target a container that failed ownership.
    }
}

function Start-NativeSidecar {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ApiKey,
        [Parameter(Mandatory = $true)][string]$HomePath,
        [Parameter(Mandatory = $true)][string]$LogPath
    )

    $sourcePath = Join-Path $RuntimeRoot "source"
    $hermesCommand = @(
        (Join-Path $sourcePath "venv\Scripts\hermes.exe"),
        (Join-Path $sourcePath "venv\Scripts\hermes.cmd"),
        (Join-Path $sourcePath "venv\Scripts\hermes")
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $hermesCommand) {
        throw "Native Hermes launcher is missing. Run install_hermes_sidecar.ps1 first."
    }

    $stdoutPath = Join-Path $LogPath "native-gateway.stdout.log"
    $stderrPath = Join-Path $LogPath "native-gateway.stderr.log"
    $childEnvironment = @{
        HERMES_HOME = $HomePath
        HERMES_SAFE_MODE = "1"
        API_SERVER_ENABLED = "true"
        API_SERVER_HOST = "127.0.0.1"
        API_SERVER_PORT = "8642"
        API_SERVER_KEY = $ApiKey
    }
    $previousValues = @{}
    foreach ($name in $childEnvironment.Keys) {
        $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, [string]$childEnvironment[$name], "Process")
    }
    try {
        $process = Start-Process -FilePath $hermesCommand `
            -ArgumentList @($Manifest.runtime.native_gateway_args) `
            -WorkingDirectory $RepoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
    } finally {
        foreach ($name in $childEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previousValues[$name], "Process")
        }
    }
    return $process
}

function Start-DockerSidecar {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ApiKey,
        [Parameter(Mandatory = $true)][string]$HomePath,
        [Parameter(Mandatory = $true)][string]$SecretPath,
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][ValidateSet("NoTools", "ProjectReadOnly")][string]$Profile,
        [string]$ReviewedConfigPath = "",
        [string]$ReviewedPolicyDirectory = "",
        [string]$ReviewedProjectRoot = "",
        [string]$ReviewedProjectId = ""
    )

    if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is not installed"
    }
    $null = & docker.exe info 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker daemon is not running"
    }
    $pinnedReference = [string]$Manifest.docker_image.pinned_reference
    $inspectJson = & docker.exe image inspect $pinnedReference 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Digest-pinned Hermes image is not installed. Run the installer only after Docker storage is moved to D:."
    }
    $image = ($inspectJson -join "`n") | ConvertFrom-Json
    $expectedDigestSuffix = "@$($Manifest.docker_image.platform_digest)"
    $matchingRepoDigests = @($image.RepoDigests | Where-Object { ([string]$_).EndsWith($expectedDigestSuffix) })
    if ($image.Os -ne "linux" -or $image.Architecture -ne "amd64" -or $matchingRepoDigests.Count -eq 0) {
        throw "Installed Hermes image does not match the reviewed linux/amd64 manifest digest"
    }

    $containerName = [string]$Manifest.runtime.container_name
    # Query the inventory instead of intentionally inspecting a missing name.
    # With ErrorActionPreference=Stop, Docker's expected "No such container"
    # stderr is promoted to a terminating PowerShell NativeCommandError.
    $existing = @(& docker.exe container ls --all --filter "name=^/$([Regex]::Escape($containerName))$" --format "{{.ID}}" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the Docker container inventory"
    }
    if (@($existing | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }).Count -ne 0) {
        throw "Hermes container '$containerName' already exists; refusing to replace or remove it automatically"
    }

    $environmentFile = Join-Path $SecretPath "docker-api.env"
    $environmentLines = @(
        "API_SERVER_KEY=$ApiKey",
        "API_SERVER_ENABLED=true",
        "API_SERVER_HOST=0.0.0.0",
        "API_SERVER_PORT=8642",
        "HERMES_HOME=/opt/data",
        "HOME=/opt/data",
        "HERMES_SAFE_MODE=1"
    )
    if ($Profile -eq "ProjectReadOnly") {
        $environmentLines += @(
            "PYTHONPATH=/opt/workbench-policy",
            "WORKBENCH_POLICY_PROFILE=project-readonly-v1",
            "WORKBENCH_PROJECT_ROOT=/workspace/project"
        )
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($environmentFile, $environmentLines, $utf8NoBom)
    Protect-SecretFile -Path $environmentFile

    $stdoutPath = Join-Path $LogPath "docker-gateway.stdout.log"
    $stderrPath = Join-Path $LogPath "docker-gateway.stderr.log"
    $dockerArgs = @(
        "run",
        "--rm",
        "--name", $containerName,
        "--env-file", $environmentFile,
        "--publish", "127.0.0.1:8642:8642",
        "--mount", "type=bind,source=${HomePath},target=/opt/data",
        "--read-only",
        "--security-opt", "no-new-privileges:true",
        "--cap-drop", "ALL",
        # The pinned image uses s6-overlay as PID 1 and must drop from root to
        # its hermes user. These are the exact minimum capabilities documented
        # by the same pinned source; everything else remains dropped.
        "--cap-add", "DAC_OVERRIDE",
        "--cap-add", "CHOWN",
        "--cap-add", "FOWNER",
        "--cap-add", "SETUID",
        "--cap-add", "SETGID",
        "--pids-limit", "256",
        "--tmpfs", "/tmp:rw,nosuid,size=512m",
        "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
        "--tmpfs", "/run:rw,exec,nosuid,size=64m"
    )
    if ($Profile -eq "ProjectReadOnly") {
        $configHash = Get-LowerSha256 -Path $ReviewedConfigPath
        $policyHash = Get-LowerSha256 -Path (Join-Path $ReviewedPolicyDirectory "sitecustomize.py")
        $rootHash = Get-LowerTextSha256 -Text $ReviewedProjectRoot.ToLowerInvariant()
        $dockerArgs += @(
            "--mount", "type=bind,source=${ReviewedConfigPath},target=/opt/data/config.yaml,readonly",
            "--mount", "type=bind,source=${ReviewedProjectRoot},target=/workspace/project,readonly",
            "--mount", "type=bind,source=${ReviewedPolicyDirectory},target=/opt/workbench-policy,readonly",
            "--label", "com.local-ai-workbench.component=hermes-sidecar",
            "--label", "com.local-ai-workbench.policy=project-readonly-v1",
            "--label", "com.local-ai-workbench.owner=workbench",
            "--label", "com.local-ai-workbench.project-id=${ReviewedProjectId}",
            "--label", "com.local-ai-workbench.project-root-sha256=${rootHash}",
            "--label", "com.local-ai-workbench.config-sha256=${configHash}",
            "--label", "com.local-ai-workbench.python-policy-sha256=${policyHash}"
        )
    } else {
        $dockerArgs += @(
            "--label", "com.local-ai-workbench.component=hermes-sidecar",
            "--label", "com.local-ai-workbench.policy=no-tools-v1",
            "--label", "com.local-ai-workbench.owner=workbench"
        )
    }
    $dockerArgs += @(
        "--pull", "never",
        $pinnedReference,
        "gateway", "run"
    )
    try {
        $process = Start-Process -FilePath "docker.exe" `
            -ArgumentList $dockerArgs `
            -WorkingDirectory $RepoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
        return $process
    } catch {
        if (Test-Path -LiteralPath $environmentFile) {
            Remove-Item -LiteralPath $environmentFile -Force
        }
        throw
    }
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Hermes sidecar manifest is missing: $ManifestPath"
}
$manifest = ([System.IO.File]::ReadAllText($ManifestPath)) | ConvertFrom-Json
Assert-ManifestAndPolicy -Manifest $manifest
if ($DeploymentMode -eq "Native" -and $ToolPolicyProfile -ne "NoTools") {
    throw "Hermes project tools are forbidden for the native sidecar"
}

$deploymentModeKey = $DeploymentMode.ToLowerInvariant()
$templateSpec = $manifest.runtime.config_templates.$deploymentModeKey
$configTemplatePath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ([string]$templateSpec.path)))
if (-not (Test-Path -LiteralPath $configTemplatePath -PathType Leaf) -or
    (Get-LowerSha256 -Path $configTemplatePath) -ne [string]$templateSpec.sha256) {
    throw "Source-controlled Hermes fail-closed config failed verification"
}
$readonlyConfigPath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ([string]$manifest.readonly_tool_policy.config_template.path)))
$readonlyPolicyFile = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ([string]$manifest.readonly_tool_policy.python_policy.path)))
$readonlyPolicyDirectory = Split-Path -Parent $readonlyPolicyFile

if ($ValidateOnly) {
    [pscustomobject]@{
        valid = $true
        deployment_mode = $DeploymentMode.ToLowerInvariant()
        endpoint = "http://127.0.0.1:8642"
        runtime_root = $RuntimeRoot
        source_commit = [string]$manifest.release.source_commit
        docker_reference = [string]$manifest.docker_image.pinned_reference
        tool_policy_profile = $ToolPolicyProfile
        tools_enabled = ($DeploymentMode -eq "Docker" -and $ToolPolicyProfile -eq "ProjectReadOnly")
        requires_os_isolation_before_tools = $true
        readiness_success_threshold = [int]$manifest.production_policy.readiness.required_consecutive_successes
        monitoring_failure_threshold = [int]$manifest.production_policy.monitoring.failure_threshold
        monitoring_restart_limit = [int]$manifest.production_policy.monitoring.max_restarts_per_launch
    }
    exit 0
}

$homePath = Join-Path $RuntimeRoot "home"
$secretPath = Join-Path $RuntimeRoot "secrets"
$logPath = Join-Path $RuntimeRoot "logs"
$runtimeConfigPath = Join-Path $homePath "config.yaml"
$apiKeyPath = Join-Path $secretPath "api_server.key"
$receiptPath = Join-Path $RuntimeRoot "install-receipt.json"
foreach ($requiredPath in @($homePath, $secretPath, $logPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Container)) {
        throw "Hermes runtime is incomplete: missing $requiredPath"
    }
}
foreach ($requiredFile in @($runtimeConfigPath, $apiKeyPath, $receiptPath)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Hermes runtime is incomplete: missing $requiredFile"
    }
}
if ((Get-LowerSha256 -Path $runtimeConfigPath) -ne [string]$templateSpec.sha256) {
    throw "Runtime Hermes config no longer matches the reviewed no-tool policy"
}
$receipt = ([System.IO.File]::ReadAllText($receiptPath)) | ConvertFrom-Json
if ([string]$receipt.deployment_mode -ne $DeploymentMode.ToLowerInvariant() -or
    [string]$receipt.source_tag -ne [string]$manifest.release.tag -or
    [string]$receipt.config_sha256 -ne [string]$templateSpec.sha256 -or
    [bool]$receipt.tools_enabled -or [bool]$receipt.mcp_enabled -or [bool]$receipt.plugins_enabled) {
    throw "Hermes installation receipt does not match the requested fail-closed deployment"
}

$reviewedProjectRoot = ""
if ($ToolPolicyProfile -eq "ProjectReadOnly") {
    if ($ProjectId -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') {
        throw "Hermes read-only project ID is invalid"
    }
    $reviewedProjectRoot = Resolve-ReviewedProjectRoot -Path $ProjectRoot
}

$apiKey = ([System.IO.File]::ReadAllText($apiKeyPath)).Trim()
if ($apiKey.Length -lt 43 -or $apiKey -match "[\r\n\s]") {
    throw "Hermes API key is malformed"
}

# The launcher reads one fixed key and intentionally leaves this alias in the
# caller's process environment. Child-only API_SERVER_KEY is scoped below.
$env:HERMES_API_SERVER_KEY = $apiKey

if (Test-PortOpen -HostName "127.0.0.1" -Port 8642) {
    throw "Port 127.0.0.1:8642 is already in use; refusing to replace an unknown process"
}

$dockerEnvironmentFile = Join-Path $secretPath "docker-api.env"
$process = $null
$startupSucceeded = $false
try {
    if ($DeploymentMode -eq "Native") {
        $process = Start-NativeSidecar -Manifest $manifest -ApiKey $apiKey -HomePath $homePath -LogPath $logPath
    } else {
        $process = Start-DockerSidecar -Manifest $manifest -ApiKey $apiKey -HomePath $homePath -SecretPath $secretPath -LogPath $logPath `
            -Profile $ToolPolicyProfile -ReviewedConfigPath $readonlyConfigPath `
            -ReviewedPolicyDirectory $readonlyPolicyDirectory -ReviewedProjectRoot $reviewedProjectRoot `
            -ReviewedProjectId $ProjectId
    }

    $baseUrl = "http://127.0.0.1:8642"
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $health = $null
    $capabilities = $null
    $toolsets = $null
    $consecutiveReady = 0
    $requiredReady = [int]$manifest.production_policy.readiness.required_consecutive_successes
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            throw "Hermes sidecar process exited during startup with code $($process.ExitCode)"
        }
        try {
            $health = Invoke-HermesEndpoint -BaseUrl $baseUrl -Path ([string]$manifest.runtime.health_path) -ApiKey $apiKey
            Assert-HermesHealth -HealthResponse $health -ReadinessPolicy $manifest.production_policy.readiness
            $capabilities = Invoke-HermesEndpoint -BaseUrl $baseUrl -Path ([string]$manifest.runtime.capabilities_path) -ApiKey $apiKey
            Assert-HermesCapabilities -CapabilitiesResponse $capabilities -ReadinessPolicy $manifest.production_policy.readiness
            $toolsets = Invoke-HermesEndpoint -BaseUrl $baseUrl -Path ([string]$manifest.runtime.toolsets_path) -ApiKey $apiKey
            Assert-HermesToolPolicy -ToolsetsResponse $toolsets -Profile $ToolPolicyProfile
            $consecutiveReady += 1
            if ($consecutiveReady -ge $requiredReady) {
                break
            }
        } catch {
            $consecutiveReady = 0
        }
        Start-Sleep -Milliseconds ([int]$manifest.production_policy.readiness.probe_interval_milliseconds)
    }
    if ($consecutiveReady -lt $requiredReady) {
        throw "Hermes sidecar did not satisfy the reviewed readiness gate within $StartupTimeoutSeconds seconds"
    }
    $startupSucceeded = $true

    $process | Add-Member -NotePropertyName HermesDeploymentMode -NotePropertyValue $DeploymentMode.ToLowerInvariant()
    $process | Add-Member -NotePropertyName HermesEndpoint -NotePropertyValue $baseUrl
    $process | Add-Member -NotePropertyName HermesApiKeyPath -NotePropertyValue $apiKeyPath
    $process | Add-Member -NotePropertyName HermesHealth -NotePropertyValue $health
    $process | Add-Member -NotePropertyName HermesCapabilities -NotePropertyValue $capabilities
    $process | Add-Member -NotePropertyName HermesToolsets -NotePropertyValue $toolsets
    $process | Add-Member -NotePropertyName HermesToolPolicyProfile -NotePropertyValue $ToolPolicyProfile
    if ($DeploymentMode -eq "Docker") {
        $process | Add-Member -NotePropertyName HermesContainerName -NotePropertyValue ([string]$manifest.runtime.container_name)
    }

    if ($PassThru) {
        $process
    } else {
        [pscustomobject]@{
            started = $true
            deployment_mode = $DeploymentMode.ToLowerInvariant()
            process_id = $process.Id
            endpoint = $baseUrl
            api_key_environment = "HERMES_API_SERVER_KEY"
            api_key_path = $apiKeyPath
            health = $health
            capabilities = $capabilities
            toolsets = $toolsets
            tool_policy_profile = $ToolPolicyProfile
            tools_enabled = ($DeploymentMode -eq "Docker" -and $ToolPolicyProfile -eq "ProjectReadOnly")
        }
    }
} finally {
    if (Test-Path -LiteralPath $dockerEnvironmentFile -PathType Leaf) {
        Remove-Item -LiteralPath $dockerEnvironmentFile -Force
    }
    if (-not $startupSucceeded -and $null -ne $process) {
        if ($DeploymentMode -eq "Docker") {
            Stop-StartedDockerSidecar -Manifest $manifest -Profile $ToolPolicyProfile
        } elseif (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
