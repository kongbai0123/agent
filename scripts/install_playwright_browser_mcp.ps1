[CmdletBinding()]
param(
    [switch]$Enable
)

$ErrorActionPreference = "Stop"
$packageVersion = "0.0.79"
$repoRoot = Split-Path -Parent $PSScriptRoot
$settingsPath = Join-Path $repoRoot "backend\settings.json"
$installRoot = Join-Path $repoRoot "runtime\tools\playwright-mcp"
$outputRoot = Join-Path $installRoot "output"

if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw "Workbench settings do not exist yet. Start Workbench once, then run this installer again."
}

$node = Get-Command node.exe -ErrorAction Stop
$nodeVersionText = (& $node.Source --version).TrimStart("v")
$nodeMajor = [int]($nodeVersionText.Split(".")[0])
if ($nodeMajor -lt 18) {
    throw "Playwright MCP requires Node.js 18 or newer."
}

$npm = Join-Path (Split-Path -Parent $node.Source) "npm.cmd"
if (-not (Test-Path -LiteralPath $npm -PathType Leaf)) {
    throw "npm.cmd was not found beside node.exe."
}

$chromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
if (-not ($chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1)) {
    throw "Google Chrome is required for the browser-playwright MCP profile."
}

New-Item -ItemType Directory -Force -Path $installRoot, $outputRoot | Out-Null
& $npm install --prefix $installRoot --save-exact --ignore-scripts "@playwright/mcp@$packageVersion"
if ($LASTEXITCODE -ne 0) {
    throw "The pinned Playwright MCP package could not be installed."
}

$packageJsonPath = Join-Path $installRoot "node_modules\@playwright\mcp\package.json"
$entrypoint = Join-Path $installRoot "node_modules\@playwright\mcp\cli.js"
$installedPackage = Get-Content -LiteralPath $packageJsonPath -Raw | ConvertFrom-Json
if ($installedPackage.version -ne $packageVersion -or -not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
    throw "The installed Playwright MCP package does not match the reviewed version."
}

$nodeSha256 = (Get-FileHash -LiteralPath $node.Source -Algorithm SHA256).Hash.ToLowerInvariant()
$policyRead = { param($risk) [ordered]@{ access = "read"; risk_level = $risk } }
$policyWrite = { param($risk) [ordered]@{ access = "write"; risk_level = $risk } }
$server = [ordered]@{
    id = "browser-playwright"
    label = "Playwright Browser"
    transport = "stdio"
    executable = $node.Source
    expected_executable_sha256 = $nodeSha256
    argv = @(
        $entrypoint,
        "--browser", "chrome",
        "--isolated",
        "--image-responses", "omit",
        "--codegen", "none",
        "--output-dir", $outputRoot
    )
    cwd = $installRoot
    allowed_cwd_roots = @($installRoot)
    environment_keys = @()
    secret_aliases = [ordered]@{}
    tool_policies = [ordered]@{
        browser_navigate = & $policyRead "external_read"
        browser_navigate_back = & $policyRead "external_read"
        browser_snapshot = & $policyRead "external_read"
        browser_find = & $policyRead "external_read"
        browser_wait_for = & $policyRead "verify"
        browser_click = & $policyWrite "external_write"
        browser_type = & $policyWrite "external_write"
        browser_press_key = & $policyWrite "external_write"
        browser_fill_form = & $policyWrite "external_write"
        browser_select_option = & $policyWrite "external_write"
        browser_handle_dialog = & $policyWrite "external_write"
        browser_tabs = & $policyWrite "external_write"
        browser_close = & $policyWrite "write"
    }
    timeout_seconds = 45
    enabled = [bool]$Enable
}

$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
$servers = @($settings.mcp_servers | Where-Object { $_.id -ne "browser-playwright" })
$settings.mcp_servers = @($servers + [pscustomobject]$server)
$serialized = $settings | ConvertTo-Json -Depth 50
$temporaryPath = "$settingsPath.$([guid]::NewGuid().ToString('N')).tmp"
try {
    [System.IO.File]::WriteAllText($temporaryPath, $serialized, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $settingsPath -Force
}
finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Host "Playwright Browser MCP $packageVersion is configured."
if ($Enable) {
    Write-Host "Restart Workbench, then select a project before asking the Agent to browse."
}
else {
    Write-Host "Open Extension Center to review, trust, and enable mcp.browser-playwright."
}
