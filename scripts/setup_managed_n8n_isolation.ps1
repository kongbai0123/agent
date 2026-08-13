[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Check", "Apply")]
    [string]$Mode = "Check",
    [string]$RuntimeRoot = "D:\llm\runtime",
    [switch]$Json,
    [switch]$ResetCredential
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security

$accountName = "WorkbenchN8n"
$credentialFileName = "n8n-service-account.dpapi.json"
$runtime = [System.IO.Path]::GetFullPath($RuntimeRoot)
$computerName = [Environment]::MachineName
$qualifiedAccount = "$computerName\$accountName"
$managedRoot = Join-Path $runtime "n8n-managed"
$credentialPath = Join-Path $runtime "secrets\$credentialFileName"
$paths = @(
    @{ Path = (Join-Path $runtime "tools\n8n"); Rights = "ReadAndExecute"; Kind = "tool" },
    @{ Path = (Join-Path $runtime "n8n-data"); Rights = "Modify"; Kind = "data" },
    @{ Path = (Join-Path $runtime "logs\n8n"); Rights = "Modify"; Kind = "log" },
    @{ Path = (Join-Path $runtime "temp\n8n"); Rights = "Modify"; Kind = "temp" },
    @{ Path = (Join-Path $runtime "cache\npm"); Rights = "Modify"; Kind = "cache" },
    @{ Path = $managedRoot; Rights = "Modify"; Kind = "managed" }
)

function ConvertTo-SidString {
    param([Parameter(Mandatory = $true)] [System.Security.Principal.IdentityReference]$Identity)
    return $Identity.Translate([System.Security.Principal.SecurityIdentifier]).Value
}

function Get-CurrentSid {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Test-IsElevated {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ServiceAccount {
    return Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue
}

function Test-IsLocalAdministrator {
    param([Parameter(Mandatory = $true)] [string]$MemberSid)
    $members = @(Get-LocalGroupMember -SID "S-1-5-32-544" -ErrorAction SilentlyContinue)
    foreach ($member in $members) {
        if ((ConvertTo-SidString -Identity $member.SID) -eq $MemberSid) { return $true }
    }
    return $false
}

function Test-SecretCredential {
    if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) { return $false }
    $password = $null
    try {
        $payload = Get-Content -LiteralPath $credentialPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$payload.schema_version -ne 1 -or [string]$payload.account -ne $qualifiedAccount) {
            return $false
        }
        $ciphertext = [Convert]::FromBase64String([string]$payload.ciphertext)
        $plain = [Security.Cryptography.ProtectedData]::Unprotect(
            $ciphertext,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        try {
            if ($plain.Length -lt 32) { return $false }
            $password = [Text.Encoding]::UTF8.GetString($plain)
            Add-Type -AssemblyName System.DirectoryServices.AccountManagement
            $context = [DirectoryServices.AccountManagement.PrincipalContext]::new(
                [DirectoryServices.AccountManagement.ContextType]::Machine,
                $computerName
            )
            try {
                return $context.ValidateCredentials(
                    $accountName,
                    $password,
                    [DirectoryServices.AccountManagement.ContextOptions]::Negotiate
                )
            }
            finally { $context.Dispose() }
        }
        finally { [Array]::Clear($plain, 0, $plain.Length) }
    }
    catch { return $false }
    finally { $password = $null }
}

function Test-ReviewedAcl {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [string]$ServiceSid,
        [Parameter(Mandatory = $true)] [string]$RequiredRights
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    try { $acl = Get-Acl -LiteralPath $Path }
    catch { return $false }
    if (-not $acl.AreAccessRulesProtected) { return $false }
    $serviceReady = $false
    $currentSid = Get-CurrentSid
    $expectedFullControl = @{
        $currentSid = $false
        "S-1-5-18" = $false
        "S-1-5-32-544" = $false
    }
    $allowedSids = @($ServiceSid, $currentSid, "S-1-5-18", "S-1-5-32-544")
    foreach ($rule in @($acl.Access)) {
        $sid = try { ConvertTo-SidString -Identity $rule.IdentityReference } catch { "" }
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if ($sid -notin $allowedSids) { return $false }
        $rights = [Security.AccessControl.FileSystemRights]$rule.FileSystemRights
        if ($sid -eq $ServiceSid) {
            $required = [Security.AccessControl.FileSystemRights]::$RequiredRights
            if (($rights -band $required) -eq $required) { $serviceReady = $true }
        }
        if ($expectedFullControl.ContainsKey($sid)) {
            $full = [Security.AccessControl.FileSystemRights]::FullControl
            if (($rights -band $full) -eq $full) { $expectedFullControl[$sid] = $true }
        }
    }
    return $serviceReady -and -not ($expectedFullControl.Values -contains $false)
}

function Get-IsolationStatus {
    $blockers = [System.Collections.Generic.List[string]]::new()
    $account = Get-ServiceAccount
    $accountExists = $null -ne $account
    $accountEnabled = $accountExists -and [bool]$account.Enabled
    $accountSid = if ($accountExists) { [string]$account.SID.Value } else { "" }
    $accountNonAdmin = $accountExists -and -not (Test-IsLocalAdministrator -MemberSid $accountSid)
    $credentialReady = Test-SecretCredential
    if (-not $accountExists) { $blockers.Add("service_account_missing") }
    elseif (-not $accountEnabled) { $blockers.Add("service_account_disabled") }
    if ($accountExists -and -not $accountNonAdmin) { $blockers.Add("service_account_is_admin") }
    if (-not $credentialReady) { $blockers.Add("launch_credential_missing") }
    $aclReady = $accountExists
    if ($accountExists) {
        foreach ($item in $paths) {
            if (-not (Test-ReviewedAcl -Path $item.Path -ServiceSid $accountSid -RequiredRights $item.Rights)) {
                $aclReady = $false
                $blockers.Add("acl_$($item.Kind)_not_reviewed")
            }
        }
        if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
            $aclReady = $false
            $blockers.Add("acl_credential_not_reviewed")
        }
        else {
            $credentialAcl = Get-Acl -LiteralPath $credentialPath
            if (-not $credentialAcl.AreAccessRulesProtected) {
                $aclReady = $false
                $blockers.Add("acl_credential_not_reviewed")
            }
            $currentSid = Get-CurrentSid
            $allowedCredentialSids = @($currentSid, "S-1-5-18", "S-1-5-32-544")
            $credentialFullControl = @{
                $currentSid = $false
                "S-1-5-18" = $false
                "S-1-5-32-544" = $false
            }
            foreach ($rule in @($credentialAcl.Access)) {
                $sid = try { ConvertTo-SidString -Identity $rule.IdentityReference } catch { "" }
                if ($rule.AccessControlType -ne "Allow") { continue }
                if ($sid -eq $accountSid -or $sid -notin $allowedCredentialSids) {
                    $aclReady = $false
                    $blockers.Add("acl_credential_not_reviewed")
                }
                if ($credentialFullControl.ContainsKey($sid)) {
                    $rights = [Security.AccessControl.FileSystemRights]$rule.FileSystemRights
                    $full = [Security.AccessControl.FileSystemRights]::FullControl
                    if (($rights -band $full) -eq $full) { $credentialFullControl[$sid] = $true }
                }
            }
            if ($credentialFullControl.Values -contains $false) {
                $aclReady = $false
                $blockers.Add("acl_credential_not_reviewed")
            }
        }
    }
    return [ordered]@{
        isolation_ready = $blockers.Count -eq 0
        blockers = @($blockers | Select-Object -Unique)
        account_exists = $accountExists
        account_enabled = $accountEnabled
        account_non_admin = $accountNonAdmin
        credential_ready = $credentialReady
        acl_ready = $aclReady
        account_sid = $accountSid
        checked_at = [DateTime]::UtcNow.ToString("o")
    }
}

function Write-Result {
    param([Parameter(Mandatory = $true)] $Payload)
    if ($Json) { $Payload | ConvertTo-Json -Compress -Depth 6 }
    else { $Payload | Format-List | Out-String | Write-Output }
}

function New-RandomPassword {
    $bytes = New-Object byte[] 48
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
        return ([Convert]::ToBase64String($bytes) + "!aA1")
    }
    finally {
        if ($null -ne $generator) { $generator.Dispose() }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Save-LaunchCredential {
    param([Parameter(Mandatory = $true)] [string]$PlainPassword)
    $bytes = [Text.Encoding]::UTF8.GetBytes($PlainPassword)
    try {
        $ciphertext = [Security.Cryptography.ProtectedData]::Protect(
            $bytes,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        $payload = [ordered]@{
            schema_version = 1
            account = $qualifiedAccount
            ciphertext = [Convert]::ToBase64String($ciphertext)
            created_at = [DateTime]::UtcNow.ToString("o")
        }
        $parent = Split-Path -Parent $credentialPath
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $temporary = "$credentialPath.tmp"
        $payload | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8 -NoNewline
        Move-Item -LiteralPath $temporary -Destination $credentialPath -Force
    }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

function Set-ReviewedAcl {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [string]$ServiceSid,
        [Parameter(Mandatory = $true)] [ValidateSet("RX", "M")] [string]$ServiceGrant
    )
    $currentSid = Get-CurrentSid
    $arguments = @(
        $Path,
        "/inheritance:r",
        "/grant:r",
        "*$($currentSid):(OI)(CI)F",
        "*$($ServiceSid):(OI)(CI)$ServiceGrant",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "/remove:g",
        "*S-1-1-0",
        "*S-1-5-11",
        "*S-1-5-32-545",
        "/C",
        "/Q"
    )
    & icacls.exe @arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ACL hardening failed for $Path" }

    # Windows PowerShell 5.1's icacls ordering can leave existing protected
    # descendants with an empty DACL when /inheritance:r and /T are combined.
    # The root above is now authoritative, so reset every existing descendant
    # to inherit only those reviewed (OI)(CI) entries.
    $descendants = Join-Path $Path "*"
    & icacls.exe $descendants "/reset" "/T" "/C" "/Q" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Descendant ACL reset failed for $Path" }
}

function Set-CredentialAcl {
    $currentSid = Get-CurrentSid
    $arguments = @(
        $credentialPath,
        "/inheritance:r",
        "/grant:r",
        "*$($currentSid):F",
        "*S-1-5-18:F",
        "*S-1-5-32-544:F",
        "/remove:g",
        "*S-1-1-0",
        "*S-1-5-11",
        "*S-1-5-32-545",
        "/Q"
    )
    & icacls.exe @arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Credential ACL hardening failed" }
}

if ($runtime -notmatch "^(?i)D:\\") {
    Write-Result ([ordered]@{
        isolation_ready = $false
        blockers = @("runtime_not_on_d_drive")
        account_exists = $false
        account_enabled = $false
        account_non_admin = $false
        credential_ready = $false
        acl_ready = $false
        account_sid = ""
        checked_at = [DateTime]::UtcNow.ToString("o")
    })
    exit 0
}

if ($Mode -eq "Check") {
    Write-Result (Get-IsolationStatus)
    exit 0
}

if ($WhatIfPreference) {
    Write-Result ([ordered]@{
        isolation_ready = $false
        blockers = @("what_if_only")
        what_if = $true
        planned_actions = @(
            "Create or enable the local WorkbenchN8n non-admin account",
            "Create a CurrentUser-DPAPI protected launch credential",
            "Restrict n8n tool, data, log, temp, cache and lifecycle ACLs"
        )
        checked_at = [DateTime]::UtcNow.ToString("o")
    })
    exit 0
}

if (-not (Test-IsElevated)) {
    throw "Apply requires an elevated Windows PowerShell session. Check and -WhatIf do not."
}

if (-not $PSCmdlet.ShouldProcess($qualifiedAccount, "Configure managed n8n isolation")) {
    exit 0
}

$account = Get-ServiceAccount
$plainPassword = $null
try {
    if ($null -eq $account) {
        $plainPassword = New-RandomPassword
        $securePassword = ConvertTo-SecureString $plainPassword -AsPlainText -Force
        New-LocalUser `
            -Name $accountName `
            -Password $securePassword `
            -AccountNeverExpires `
            -PasswordNeverExpires `
            -UserMayNotChangePassword `
            -Description "Low-privilege Workbench n8n account" | Out-Null
        $account = Get-ServiceAccount
    }
    elseif (-not $account.Enabled) {
        Enable-LocalUser -Name $accountName
        $account = Get-ServiceAccount
    }

    $accountSid = [string]$account.SID.Value
    if (Test-IsLocalAdministrator -MemberSid $accountSid) {
        throw "WorkbenchN8n is an Administrator. Remove that membership explicitly before applying."
    }

    if (-not (Test-SecretCredential)) {
        if ($null -eq $plainPassword -and -not $ResetCredential) {
            throw "The account exists but its protected launch credential is missing. Re-run with -ResetCredential to rotate it."
        }
        if ($null -eq $plainPassword) {
            $plainPassword = New-RandomPassword
            $securePassword = ConvertTo-SecureString $plainPassword -AsPlainText -Force
            Set-LocalUser -Name $accountName -Password $securePassword
        }
        Save-LaunchCredential -PlainPassword $plainPassword
    }

    foreach ($item in $paths) {
        New-Item -ItemType Directory -Path $item.Path -Force | Out-Null
        $grant = if ($item.Rights -eq "ReadAndExecute") { "RX" } else { "M" }
        Set-ReviewedAcl -Path $item.Path -ServiceSid $accountSid -ServiceGrant $grant
    }
    Set-CredentialAcl
}
finally {
    $plainPassword = $null
}

$result = Get-IsolationStatus
Write-Result $result
if (-not $result.isolation_ready) { exit 2 }
