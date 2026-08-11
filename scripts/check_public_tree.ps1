[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$allowedRuntimeFiles = @(
    "runtime/server-discovery-config.json.example"
)

Push-Location $repositoryRoot
try {
    $trackedFiles = @(& git ls-files)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the Git tracked-file list."
    }

    $violations = foreach ($trackedFile in $trackedFiles) {
        $path = $trackedFile.Replace("\", "/")
        $isAllowedRuntimeExample = $allowedRuntimeFiles -contains $path
        $isPrivatePath =
            $path -match "(^|/)(projects|conversations|sessions|attachments|turns|messages|runs|workspaces|artifacts|archive)(/|$)" -or
            $path -match "(^|/)(backend/)?data(/|$)" -or
            ($path -match "^runtime/" -and -not $isAllowedRuntimeExample)
        $isPrivateFile =
            $path -eq "backend/settings.json" -or
            $path -match "(^|/)\.env(\..+)?$" -or
            $path -match "\.(db|sqlite|sqlite3|jsonl|log|pem|key)$"

        if ($isPrivatePath -or $isPrivateFile) {
            $path
        }
    }

    if ($violations) {
        Write-Error (
            "Public-tree check failed. Remove these local/private files from Git:`n - " +
            ($violations -join "`n - ")
        )
    }

    $secretPattern = (
        "(^|[^A-Za-z0-9_-])" +
        "(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|" +
        "AIza[0-9A-Za-z_-]{20,}|nvapi-[A-Za-z0-9_-]{32,})|" +
        "-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|" +
        "https?://[^[:space:]/:@]+:[^[:space:]@]+@"
    )
    $potentialSecretFiles = @(
        & git grep -l -I -E $secretPattern -- . ":!*.example" ":!*.svg"
    )
    if ($LASTEXITCODE -gt 1) {
        throw "Unable to scan the Git tree for sensitive values."
    }
    if ($potentialSecretFiles) {
        Write-Error (
            "Public-tree check found potential sensitive values in:`n - " +
            ($potentialSecretFiles -join "`n - ")
        )
    }

    Write-Host (
        (
            "Public-tree check passed: {0} tracked files, no project, chat, runtime, " +
            "database, log, local-secret files, or credential-shaped values detected."
        ) -f $trackedFiles.Count
    )
    exit 0
}
finally {
    Pop-Location
}
