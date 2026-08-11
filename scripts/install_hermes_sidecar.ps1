[CmdletBinding()]
param(
    [ValidateSet("Native", "Docker")]
    [string]$DeploymentMode = "Native",

    [string]$RuntimeRoot = "",
    [string]$ManifestPath = "",

    [switch]$ValidateOnly,
    [switch]$ForceConfig,

    # Docker pulls are deliberately opt-in. The script also refuses to pull
    # while Docker Desktop's large WSL data disk is still on the system drive.
    [switch]$PullDockerImage
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
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Utf8NoBomText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Assert-HermesManifest {
    param([Parameter(Mandatory = $true)]$Manifest)

    if ($Manifest.schema_version -ne 1) {
        throw "Unsupported Hermes manifest schema: $($Manifest.schema_version)"
    }
    if ($Manifest.release.package_version -ne "0.18.2" -or
        $Manifest.release.tag -ne "v2026.7.7.2" -or
        $Manifest.release.source_commit -ne "9de9c25f620ff7f1ce0fd5457d596052d5159596") {
        throw "Hermes release pin does not match the reviewed v0.18.2 source release"
    }
    if ($Manifest.docker_image.index_digest -ne "sha256:9c841866021c54c4596849f6135717e8a4d52ba510b7f52c50aef1de1a283973" -or
        $Manifest.docker_image.platform_digest -ne "sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a") {
        throw "Hermes Docker digest pin does not match the reviewed official image"
    }

    foreach ($modeKey in @("native", "docker")) {
        $templateSpec = $Manifest.runtime.config_templates.$modeKey
        $templatePath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ([string]$templateSpec.path)))
        if (-not $templatePath.StartsWith($RepoRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Config template escapes the repository root"
        }
        if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
            throw "Hermes $modeKey config template is missing: $templatePath"
        }
        $actualConfigHash = Get-LowerSha256 -Path $templatePath
        if ($actualConfigHash -ne [string]$templateSpec.sha256) {
            throw "Hermes $modeKey config template hash mismatch: expected $($templateSpec.sha256), got $actualConfigHash"
        }
    }

    if ([string]$Manifest.runtime.host -ne "127.0.0.1" -or [int]$Manifest.runtime.host_port -ne 8642) {
        throw "Hermes host binding must remain 127.0.0.1:8642"
    }
    if ([string]$Manifest.runtime.container_host -ne "0.0.0.0") {
        throw "Hermes container must listen on 0.0.0.0 behind the loopback-only Docker publish"
    }
    if ([string]$Manifest.model.base_urls.native -ne "http://127.0.0.1:11434/v1" -or
        [string]$Manifest.model.base_urls.docker -ne "http://host.docker.internal:11434/v1") {
        throw "Hermes mode-specific local-model endpoint pins changed unexpectedly"
    }
    if ([int]$Manifest.model.context_length -ne 64000 -or
        [int]$Manifest.model.max_output_tokens -ne 4096) {
        throw "Hermes context or output-reserve contract changed unexpectedly"
    }
    if (@($Manifest.initial_policy.disabled_toolsets).Count -lt 25) {
        throw "Hermes fail-closed policy no longer disables every reviewed core toolset"
    }
    if (@($Manifest.initial_policy.platform_toolsets.api_server).Count -ne 0 -or
        @($Manifest.initial_policy.mcp_servers.psobject.Properties).Count -ne 0 -or
        @($Manifest.initial_policy.plugins.enabled).Count -ne 0) {
        throw "Hermes initial tool, MCP, and plugin policy must remain empty"
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

    $selectedMode = $DeploymentMode.ToLowerInvariant()
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot ([string]$Manifest.runtime.config_templates.$selectedMode.path)))
}

function Protect-SecretPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Directory
    )

    if ($env:OS -ne "Windows_NT") {
        return
    }
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $grant = if ($Directory) { "*$($sid):(OI)(CI)F" } else { "*$($sid):(F)" }
    $null = & icacls.exe $Path /inheritance:r /grant:r $grant 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict ACL on Hermes secret path: $Path"
    }
}

function Initialize-HermesRuntimeFiles {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ConfigTemplate
    )

    $selectedConfigHash = Get-LowerSha256 -Path $ConfigTemplate
    $homePath = Join-Path $RuntimeRoot "home"
    $secretPath = Join-Path $RuntimeRoot "secrets"
    $logPath = Join-Path $RuntimeRoot "logs"
    foreach ($path in @($RuntimeRoot, $homePath, $secretPath, $logPath, (Join-Path $RuntimeRoot "cache"), (Join-Path $RuntimeRoot "temp"))) {
        if (-not (Test-Path -LiteralPath $path)) {
            $null = New-Item -ItemType Directory -Path $path -Force
        }
    }

    Protect-SecretPath -Path $secretPath -Directory
    $apiKeyPath = Join-Path $secretPath "api_server.key"
    if (-not (Test-Path -LiteralPath $apiKeyPath -PathType Leaf)) {
        $keyBytes = New-Object byte[] ([int]$Manifest.security.minimum_api_key_bytes)
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $rng.GetBytes($keyBytes)
        } finally {
            $rng.Dispose()
        }
        $apiKey = [Convert]::ToBase64String($keyBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($apiKeyPath, $apiKey, $utf8NoBom)
        Protect-SecretPath -Path $apiKeyPath
    } else {
        $apiKey = ([System.IO.File]::ReadAllText($apiKeyPath)).Trim()
        if ($apiKey.Length -lt 43 -or $apiKey -match "[\r\n\s]") {
            throw "Existing Hermes API key is malformed; refusing to replace it automatically"
        }
        Protect-SecretPath -Path $apiKeyPath
    }

    $runtimeConfig = Join-Path $homePath "config.yaml"
    if (Test-Path -LiteralPath $runtimeConfig -PathType Leaf) {
        $existingHash = Get-LowerSha256 -Path $runtimeConfig
        if ($existingHash -ne $selectedConfigHash -and -not $ForceConfig) {
            throw "Existing Hermes config differs from the fail-closed template. Re-run with -ForceConfig only after reviewing it."
        }
    }
    if (-not (Test-Path -LiteralPath $runtimeConfig -PathType Leaf) -or $ForceConfig) {
        [System.IO.File]::WriteAllBytes($runtimeConfig, [System.IO.File]::ReadAllBytes($ConfigTemplate))
    }
    if ((Get-LowerSha256 -Path $runtimeConfig) -ne $selectedConfigHash) {
        throw "Runtime Hermes config failed its post-copy hash check"
    }

    return [pscustomobject]@{
        Home = $homePath
        ApiKeyPath = $apiKeyPath
        ConfigPath = $runtimeConfig
        LogPath = $logPath
    }
}

function Invoke-OfficialInstallerStage {
    param(
        [Parameter(Mandatory = $true)][string]$PowerShellExe,
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$HermesHome,
        [Parameter(Mandatory = $true)][string]$InstallDir
    )

    Write-Host "[Hermes] official installer stage: $Stage"
    # Native programs commonly write progress to stderr even on success (git
    # clone does this). Temporarily relax EAP and judge only the child exit code.
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $stageOutput = & $PowerShellExe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
            -File $InstallerPath `
            -Stage $Stage `
            -NonInteractive `
            -Json `
            -SkipSetup `
            -Branch ([string]$Manifest.release.tag) `
            -Commit ([string]$Manifest.release.source_commit) `
            -HermesHome $HermesHome `
            -InstallDir $InstallDir 2>&1
        $stageExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    foreach ($line in @($stageOutput)) {
        Write-Host $line
    }
    if ($stageExitCode -ne 0) {
        throw "Official Hermes installer stage '$Stage' failed with exit code $stageExitCode"
    }
}

function Install-NativeHermes {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)]$RuntimeFiles
    )

    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        throw "Git for Windows must already be installed; this controlled installer will not install system packages"
    }

    $bootstrapPath = Join-Path $RuntimeRoot "bootstrap"
    if (-not (Test-Path -LiteralPath $bootstrapPath)) {
        $null = New-Item -ItemType Directory -Path $bootstrapPath -Force
    }
    $installerPath = Join-Path $bootstrapPath "install-v2026.7.7.2.ps1"
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        Write-Host "[Hermes] downloading the official, tag-pinned Windows installer"
        $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = $previousProtocol -bor [Net.SecurityProtocolType]::Tls12
            $webClient = New-Object System.Net.WebClient
            try {
                $webClient.DownloadFile([string]$Manifest.official_installer.url, $installerPath)
            } finally {
                $webClient.Dispose()
            }
        } finally {
            [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
        }
    }
    $installerHash = Get-LowerSha256 -Path $installerPath
    if ($installerHash -ne [string]$Manifest.official_installer.sha256) {
        throw "Official Hermes installer SHA-256 mismatch: expected $($Manifest.official_installer.sha256), got $installerHash"
    }

    $sourcePath = Join-Path $RuntimeRoot "source"
    $cacheRoot = Join-Path $RuntimeRoot "cache"
    # The upstream Windows installer rewrites GIT_CONFIG_COUNT during its
    # repository stage. Point its --global writes at an isolated runtime file
    # so the system core.autocrlf=true setting cannot dirty the managed clone
    # and the user's real global Git configuration remains untouched.
    $gitConfigPath = Join-Path $RuntimeRoot "installer.gitconfig"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines(
        $gitConfigPath,
        @(
            "[core]",
            "`tautocrlf = false",
            "[windows]",
            "`tappendAtomically = false"
        ),
        $utf8NoBom
    )
    $scopedValues = @{
        HERMES_HOME = $RuntimeFiles.Home
        UV_CACHE_DIR = (Join-Path $cacheRoot "uv")
        UV_INSTALL_DIR = (Join-Path $RuntimeFiles.Home "bin")
        UV_PYTHON_INSTALL_DIR = (Join-Path $RuntimeRoot "python")
        UV_PYTHON_BIN_DIR = (Join-Path $RuntimeRoot "python-bin")
        UV_TOOL_DIR = (Join-Path $RuntimeRoot "uv-tools")
        UV_TOOL_BIN_DIR = (Join-Path $RuntimeRoot "uv-tool-bin")
        UV_NO_MODIFY_PATH = "1"
        # Git for Windows ships core.autocrlf=true in its system config. Force
        # LF at clone time without changing global/system Git configuration.
        GIT_CONFIG_COUNT = "1"
        GIT_CONFIG_KEY_0 = "core.autocrlf"
        GIT_CONFIG_VALUE_0 = "false"
        GIT_CONFIG_GLOBAL = $gitConfigPath
        XDG_CACHE_HOME = $cacheRoot
        PIP_CACHE_DIR = (Join-Path $cacheRoot "pip")
        TEMP = (Join-Path $RuntimeRoot "temp")
        TMP = (Join-Path $RuntimeRoot "temp")
    }
    $previousValues = @{}
    foreach ($name in $scopedValues.Keys) {
        $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, [string]$scopedValues[$name], "Process")
    }

    $legacyHome = Join-Path $env:LOCALAPPDATA "hermes"
    $legacyStampBefore = if (Test-Path -LiteralPath $legacyHome) { (Get-Item -LiteralPath $legacyHome).LastWriteTimeUtc.Ticks } else { $null }
    $userPathBefore = [Environment]::GetEnvironmentVariable("Path", "User")
    try {
        $powerShellExe = (Get-Process -Id $PID).Path
        foreach ($stage in @($Manifest.official_installer.native_stages)) {
            Invoke-OfficialInstallerStage -PowerShellExe $powerShellExe -InstallerPath $installerPath `
                -Stage ([string]$stage) -Manifest $Manifest -HermesHome $RuntimeFiles.Home -InstallDir $sourcePath
        }
    } finally {
        foreach ($name in $scopedValues.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previousValues[$name], "Process")
        }
    }

    $userPathAfter = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPathAfter -ne $userPathBefore) {
        throw "The official installer changed the user PATH despite the controlled stage allowlist"
    }
    $legacyStampAfter = if (Test-Path -LiteralPath $legacyHome) { (Get-Item -LiteralPath $legacyHome).LastWriteTimeUtc.Ticks } else { $null }
    if ($legacyStampAfter -ne $legacyStampBefore) {
        throw "The legacy LOCALAPPDATA Hermes directory changed; refusing this installation result"
    }

    $installedCommit = (& git.exe -C $sourcePath rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $installedCommit -ne [string]$Manifest.release.source_commit) {
        throw "Installed Hermes source commit mismatch: $installedCommit"
    }
    $trackedChanges = @(& git.exe -C $sourcePath status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0 -or $trackedChanges.Count -ne 0) {
        throw "Installed Hermes source worktree is not clean: $($trackedChanges.Count) tracked changes"
    }
    $pyprojectPath = Join-Path $sourcePath "pyproject.toml"
    $pyprojectText = [System.IO.File]::ReadAllText($pyprojectPath)
    if ($pyprojectText -notmatch '(?m)^version\s*=\s*"0\.18\.2"\s*$') {
        throw "Installed Hermes pyproject version is not 0.18.2"
    }

    $hermesCommand = @(
        (Join-Path $sourcePath "venv\Scripts\hermes.exe"),
        (Join-Path $sourcePath "venv\Scripts\hermes.cmd"),
        (Join-Path $sourcePath "venv\Scripts\hermes")
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $hermesCommand) {
        throw "Hermes launcher is missing from the isolated virtual environment"
    }

    $validationEnvironment = @{
        HERMES_HOME = $RuntimeFiles.Home
        HERMES_SAFE_MODE = "1"
    }
    $validationPrevious = @{}
    foreach ($name in $validationEnvironment.Keys) {
        $validationPrevious[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, [string]$validationEnvironment[$name], "Process")
    }
    try {
        $versionOutput = & $hermesCommand --version 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Hermes --version failed: $($versionOutput -join ' ')"
        }
        $configOutput = & $hermesCommand config check 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Hermes config check rejected the isolated config: $($configOutput -join ' ')"
        }
    } finally {
        foreach ($name in $validationEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $validationPrevious[$name], "Process")
        }
    }

    $receipt = [ordered]@{
        schema_version = 1
        deployment_mode = "native"
        installed_at_utc = [DateTime]::UtcNow.ToString("o")
        package_version = [string]$Manifest.release.package_version
        source_tag = [string]$Manifest.release.tag
        source_commit = $installedCommit
        installer_sha256 = $installerHash
        pyproject_sha256 = Get-LowerSha256 -Path $pyprojectPath
        launcher_path = $hermesCommand
        launcher_sha256 = Get-LowerSha256 -Path $hermesCommand
        hermes_home = $RuntimeFiles.Home
        config_sha256 = Get-LowerSha256 -Path $RuntimeFiles.ConfigPath
        api_key_path = $RuntimeFiles.ApiKeyPath
        user_path_unchanged = $true
        legacy_localappdata_unchanged = $true
        git_global_config_isolated = $true
        source_worktree_clean = $true
        tools_enabled = $false
        mcp_enabled = $false
        plugins_enabled = $false
    }
    $receiptPath = Join-Path $RuntimeRoot "install-receipt.json"
    Write-Utf8NoBomText -Path $receiptPath -Text ($receipt | ConvertTo-Json -Depth 8)
    return [pscustomobject]$receipt
}

function Install-DockerHermes {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)]$RuntimeFiles
    )

    if (-not $PullDockerImage) {
        return [pscustomobject]@{
            schema_version = 1
            deployment_mode = "docker"
            prepared = $true
            image_pulled = $false
            pinned_reference = [string]$Manifest.docker_image.pinned_reference
            reason = "Docker pull is opt-in and remains blocked until Docker Desktop storage is moved off the system drive."
        }
    }

    $dockerVhd = Join-Path $env:LOCALAPPDATA "Docker\wsl\disk\docker_data.vhdx"
    if (Test-Path -LiteralPath $dockerVhd -PathType Leaf) {
        $vhdBytes = (Get-Item -LiteralPath $dockerVhd).Length
        if ($vhdBytes -gt 1GB) {
            throw "Refusing Docker pull: Docker Desktop data disk is still on the system drive ($dockerVhd, $([Math]::Round($vhdBytes / 1GB, 1)) GiB)."
        }
    }
    if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is not installed"
    }
    $null = & docker.exe info 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker daemon is not running"
    }

    $tagReference = "$($Manifest.docker_image.repository):$($Manifest.docker_image.tag)"
    $indexInspection = & docker.exe buildx imagetools inspect $tagReference 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to verify the official Docker tag index digest: $($indexInspection -join ' ')"
    }
    $indexText = $indexInspection -join "`n"
    if ($indexText -notmatch [Regex]::Escape([string]$Manifest.docker_image.index_digest)) {
        throw "Docker tag index digest no longer matches the reviewed release"
    }

    $pinnedReference = [string]$Manifest.docker_image.pinned_reference
    Write-Host "[Hermes] pulling the digest-pinned linux/amd64 image"
    $pullOutput = & docker.exe pull --platform linux/amd64 $pinnedReference 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Digest-pinned Docker pull failed: $($pullOutput -join ' ')"
    }
    $inspectOutput = & docker.exe image inspect $pinnedReference 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Pulled Hermes image could not be inspected"
    }
    $image = ($inspectOutput -join "`n") | ConvertFrom-Json
    if ($image.Os -ne "linux" -or $image.Architecture -ne "amd64") {
        throw "Hermes image platform mismatch: $($image.Os)/$($image.Architecture)"
    }
    $expectedDigestSuffix = "@$($Manifest.docker_image.platform_digest)"
    $matchingRepoDigests = @($image.RepoDigests | Where-Object { ([string]$_).EndsWith($expectedDigestSuffix) })
    if ($matchingRepoDigests.Count -eq 0) {
        throw "Hermes image RepoDigests does not contain the reviewed linux/amd64 manifest digest"
    }

    $receipt = [ordered]@{
        schema_version = 1
        deployment_mode = "docker"
        installed_at_utc = [DateTime]::UtcNow.ToString("o")
        package_version = [string]$Manifest.release.package_version
        source_tag = [string]$Manifest.release.tag
        index_digest = [string]$Manifest.docker_image.index_digest
        platform_digest = [string]$Manifest.docker_image.platform_digest
        pinned_reference = $pinnedReference
        image_id = [string]$image.Id
        platform = "$($image.Os)/$($image.Architecture)"
        hermes_home = $RuntimeFiles.Home
        config_sha256 = Get-LowerSha256 -Path $RuntimeFiles.ConfigPath
        api_key_path = $RuntimeFiles.ApiKeyPath
        tools_enabled = $false
        mcp_enabled = $false
        plugins_enabled = $false
    }
    $receiptPath = Join-Path $RuntimeRoot "install-receipt.json"
    Write-Utf8NoBomText -Path $receiptPath -Text ($receipt | ConvertTo-Json -Depth 8)
    return [pscustomobject]$receipt
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Hermes sidecar manifest is missing: $ManifestPath"
}
$manifest = ([System.IO.File]::ReadAllText($ManifestPath)) | ConvertFrom-Json
$configTemplatePath = Assert-HermesManifest -Manifest $manifest
$deploymentModeKey = $DeploymentMode.ToLowerInvariant()

if ($ValidateOnly) {
    [pscustomobject]@{
        valid = $true
        deployment_mode = $DeploymentMode.ToLowerInvariant()
        release = "$($manifest.release.package_version) ($($manifest.release.tag))"
        source_commit = [string]$manifest.release.source_commit
        docker_reference = [string]$manifest.docker_image.pinned_reference
        runtime_root = $RuntimeRoot
        config_sha256 = [string]$manifest.runtime.config_templates.$deploymentModeKey.sha256
        tools_enabled = $false
    }
    exit 0
}

$runtimeFiles = Initialize-HermesRuntimeFiles -Manifest $manifest -ConfigTemplate $configTemplatePath
if ($DeploymentMode -eq "Native") {
    Install-NativeHermes -Manifest $manifest -RuntimeFiles $runtimeFiles
} else {
    Install-DockerHermes -Manifest $manifest -RuntimeFiles $runtimeFiles
}
