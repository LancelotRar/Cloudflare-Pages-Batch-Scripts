<#
.SYNOPSIS
    Multi-account Cloudflare Pages manager.
.DESCRIPTION
    Reads .env for multi-account config, then provides interactive menu for:
    - Batch delete projects and custom domains (fetches real-time state from CF)
    - Batch create projects and set custom domains (from .env configuration)
    - Full workflow: delete old → create new → set domain
.EXAMPLE
    .\deploy.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# ---- Color output helpers ----
function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Cyan }
function Write-Ok   { Write-Host "[OK]    $args" -ForegroundColor Green }
function Write-Warn { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Write-Err  { Write-Host "[ERROR] $args" -ForegroundColor Red }

# ---- Cloudflare REST API helper ----
function Invoke-CfApi {
    param([string]$Method, [string]$Uri, [string]$Token, [object]$Body)
    $headers = @{'Authorization' = "Bearer $Token"; 'Content-Type' = 'application/json'}
    $params = @{ Method = $Method; Uri = $Uri; Headers = $headers; UseBasicParsing = $true }
    if ($Body) { $params['Body'] = ($Body | ConvertTo-Json -Depth 5 -Compress) }
    try { return Invoke-RestMethod @params }
    catch { Write-Err "API call failed: $_"; return $null }
}

# ================================================================
# Management functions - batch operations across multi-account .env
# ================================================================

function Get-Accounts {
    <#
    .SYNOPSIS
        Parse .env into account object array (shared by all operations)
    #>
    $envPath = Join-Path -Path $PSScriptRoot -ChildPath '.env'
    if (-not (Test-Path -LiteralPath $envPath)) { Write-Err '.env not found'; return $null }

    $lines       = Get-Content -LiteralPath $envPath -Encoding UTF8
    $rawAccounts = [ordered]@{}
    $currentKey  = $null

    :envline foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed -match '^#') { continue }

        if ($trimmed -match '^(CF_[^_]+)_NAME=(.+)') {
            $currentKey = $Matches[1]
            $rawAccounts[$currentKey] = @{
                Id            = $currentKey
                Name          = $Matches[2]
                Token         = $null
                AccountId     = $null
                Project       = $null
                ProjectType   = 'production'
                CurrentDomain = $null
                NewProject    = ''
                NewDomain     = ''
                KvvNamespaceId = $null
                KvvBinding    = 'KV'
                Vars          = [ordered]@{}
            }
            continue
        }
        if (-not $currentKey) { continue }

        switch -Regex ($trimmed) {
            "^${currentKey}_TOKEN=(.*)"               { $rawAccounts[$currentKey].Token         = $Matches[1]; continue envline }
            "^${currentKey}_ACCOUNT_ID=(.*)"           { $rawAccounts[$currentKey].AccountId     = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_PROJECT_NAME=(.*)"   { $rawAccounts[$currentKey].Project       = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_PROJECT_TYPE=(.*)"   { $rawAccounts[$currentKey].ProjectType    = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_CURRENT_DOMAIN=(.*)" { $rawAccounts[$currentKey].CurrentDomain   = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_NEW_PROJECT_NAME=(.*)" { $rawAccounts[$currentKey].NewProject   = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_NEW_DOMAIN=(.*)"     { $rawAccounts[$currentKey].NewDomain      = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_KV_NAMESPACE_ID=(.*)" { $rawAccounts[$currentKey].KvvNamespaceId = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_KV_BINDING=(.*)"     { $rawAccounts[$currentKey].KvvBinding    = $Matches[1]; continue envline }
        }

        if ($trimmed -match "^${currentKey}_(.+)_TYPE=(.*)") {
            $rawAccounts[$currentKey].Vars[$Matches[1]] = @{ type = $Matches[2]; value = $null }
            continue
        }
        foreach ($known in $rawAccounts[$currentKey].Vars.Keys) {
            if ($trimmed -match "^${currentKey}_${known}=(.*)") {
                $rawAccounts[$currentKey].Vars[$known].value = $Matches[1]
                break
            }
        }
    }

    return $rawAccounts.Values | Where-Object { $_.Token -and $_.AccountId -and $_.Project } | Sort-Object Name
}

function Select-Accounts {
    <#
    .SYNOPSIS
        Interactive multi-account selection.  Returns selected accounts array.
        Pass -All to skip prompt and select all.
    #>
    param([switch]$All)
    $accounts = Get-Accounts
    if (-not $accounts) { return $null }
    if ($accounts.Count -eq 0) { Write-Err 'No valid accounts found'; return $null }

    if ($All) { Write-Info "All $($accounts.Count) account(s) selected"; return $accounts }

    $null = try { Clear-Host } catch { }
    Write-Host '===================== Account list =====================' -ForegroundColor Yellow
    for ($i = 0; $i -lt $accounts.Count; $i++) {
        $a    = $accounts[$i]
        $vars = ($a.Vars.Keys | ForEach-Object { "$_ = $($a.Vars[$_].value)" }) -join ', '
        $domainInfo = if ($a.CurrentDomain) { ", domain=$($a.CurrentDomain)" } else { '' }
        $newInfo    = if ($a.NewProject -or $a.NewDomain) { ", new=$($a.NewProject)$(if($a.NewDomain){' / '.$a.NewDomain})" } else { '' }
        Write-Host "  [$($i+1)] $($a.Name)  ->  $($a.Project)$domainInfo$newInfo"
    }
    Write-Host '========================================================' -ForegroundColor Yellow
    Write-Host '  [A]ll accounts'
    Write-Host '  [Q]uit'
    Write-Host ''

    $sel = Read-Host 'Selection'
    switch -Regex ($sel) {
        '^[Qq]$' { return $null }
        '^[Aa]$' { return $accounts }
        default  {
            $result = @()
            $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
                $n = [int]$_
                if ($n -ge 1 -and $n -le $accounts.Count) { $result += $accounts[$n - 1] }
                else { Write-Warn "Skipping invalid index: $_" }
            }
            if ($result.Count -eq 0) { Write-Err 'No valid account selected'; return $null }
            return $result
        }
    }
}

function Sync-EnvState {
    <#
    .SYNOPSIS
        Pull actual Cloudflare Pages project state into .env.
        Detects project names, custom domains, and updates the file.
    #>
    Write-Info 'Syncing .env with Cloudflare state ...'

    $envPath       = Join-Path -Path $PSScriptRoot -ChildPath '.env'
    $envContent    = Get-Content -LiteralPath $envPath -Encoding UTF8
    $accounts      = Get-Accounts
    if (-not $accounts) { return }

    $updatedLines  = @()
    $changed       = $false

    foreach ($line in $envContent) {
        $trimmed = $line.Trim()
        $matched = $false

        if ($trimmed -match '^(CF_[^_]+)_PAGES_PROJECT_NAME=(.*)') {
            $key = $Matches[1]
            $acct = $accounts | Where-Object { $_.Id -eq $key } | Select-Object -First 1
            if ($acct) {
                Write-Info "  Checking $key ($($acct.Name)) ..."
                $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
                if ($resp -and $resp.success) {
                    $project = $resp.result | Select-Object -First 1
                    if ($project) {
                        $actualName = $project.name
                        $actualDomains = ($project.domains | Where-Object { $_ -ne "$actualName.pages.dev" }) -join ', '
                        $actualKv = $project.deployment_configs.production.kv_namespaces
                        $kvLine = ''
                        if ($actualKv) {
                            $kvBinding = ($actualKv.PSObject.Properties | Select-Object -First 1)
                            if ($kvBinding) {
                                $kvLine = "CF_${key}_PAGES_KV_NAMESPACE_ID=$($kvBinding.Value.namespace_id)"
                                $updatedLines += "CF_${key}_PAGES_KV_BINDING=$($kvBinding.Name)"
                            }
                        }
                        if ($actualName -ne $acct.Project) {
                            Write-Warn "  Project name mismatch: .env=$($acct.Project), actual=$actualName"
                        }
                        $updatedLines += "CF_${key}_PAGES_PROJECT_NAME=$actualName"
                        $updatedLines += "CF_${key}_PAGES_CURRENT_DOMAIN=$actualDomains"
                        if ($kvLine) { $updatedLines += $kvLine }
                        Write-Ok "  ${key}: project=$actualName, domain=$actualDomains"
                        $changed = $true
                        $matched = $true
                    }
                } else {
                    $updatedLines += $line  # keep original
                }
            } else {
                $updatedLines += $line
            }
            # Skip the following lines for this key (CURRENT_DOMAIN, NEW_PROJECT_NAME, NEW_DOMAIN)
            # We'll regenerate them
            continue
        }

        # Skip CURRENT_DOMAIN, NEW_*, KV_* lines - they'll be regenerated
        if ($trimmed -match '^CF_[^_]+_PAGES_CURRENT_DOMAIN=') { continue }
        if ($trimmed -match '^CF_[^_]+_PAGES_NEW_PROJECT_NAME=') { continue }
        if ($trimmed -match '^CF_[^_]+_PAGES_NEW_DOMAIN=') { continue }
        if ($trimmed -match '^CF_[^_]+_PAGES_KV_') { continue }

        $updatedLines += $line
    }

    # Append NEW_* and KV_* fields for any accounts that don't have them yet
    foreach ($acct in $accounts) {
        if (-not ($envContent | Where-Object { $_ -match "^${acct.Id}_PAGES_NEW_PROJECT_NAME=" } | Select-Object -First 1)) {
            $updatedLines += "CF_$($acct.Id)_PAGES_NEW_PROJECT_NAME="
            $updatedLines += "CF_$($acct.Id)_PAGES_NEW_DOMAIN="
            $changed = $true
        }
        if (-not ($envContent | Where-Object { $_ -match "^${acct.Id}_PAGES_KV_NAMESPACE_ID=" } | Select-Object -First 1)) {
            $updatedLines += "CF_$($acct.Id)_PAGES_KV_NAMESPACE_ID="
            $updatedLines += "CF_$($acct.Id)_PAGES_KV_BINDING=KV"
            $changed = $true
        }
    }

    if ($changed) {
        $updatedLines -replace "`r",'' | Set-Content -LiteralPath $envPath -Encoding UTF8 -NoNewline
        Write-Ok '.env updated with current Cloudflare state'
    } else {
        Write-Info '.env is already in sync'
    }
}

function Remove-CustomDomains {
    <#
    .SYNOPSIS
        Query Cloudflare for actual projects/domains, let user pick which to delete.
        Uses .env only for credentials (token, account_id).
    #>
    $accounts = Get-Accounts
    if (-not $accounts) { return }

    Write-Host "`n========== Fetching actual domains from Cloudflare ==========" -ForegroundColor Yellow

    # Collect all domains across all accounts
    $domainItems = @()  # each: @{ Index, AccountName, AccountId, Token, ProjectName, DomainName }
    $globalIdx = 0

    foreach ($acct in $accounts) {
        Write-Info "Querying $($acct.Name) ..."
        $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
        if (-not $resp -or -not $resp.success) { Write-Warn "  Skipping $($acct.Name) - API error"; continue }

        foreach ($project in $resp.result) {
            $projName = $project.name
            $customDomains = $project.domains | Where-Object { $_ -ne "$projName.pages.dev" }
            foreach ($d in $customDomains) {
                $globalIdx++
                $domainItems += [PSCustomObject]@{
                    Index       = $globalIdx
                    AccountName = $acct.Name
                    AccountId   = $acct.AccountId
                    Token       = $acct.Token
                    ProjectName = $projName
                    DomainName  = $d
                }
            }
        }
    }

    if ($domainItems.Count -eq 0) { Write-Info 'No custom domains found on Cloudflare'; return }

    # Show selection
    Write-Host "`nFound $($domainItems.Count) custom domain(s):" -ForegroundColor Cyan
    foreach ($item in $domainItems) {
        Write-Host "  [$($item.Index)] $($item.AccountName) | $($item.ProjectName) | $($item.DomainName)" -ForegroundColor White
    }
    Write-Host '  [A]ll'
    Write-Host '  [Q]uit'
    Write-Host ''

    $sel = Read-Host "Enter number(s) to delete (e.g. '1,3' or '1-3')"
    if ($sel -match '^[Qq]$') { Write-Info 'Cancelled'; return }

    $selectedItems = @()
    if ($sel -match '^[Aa]$') {
        $selectedItems = $domainItems
    } else {
        # Parse ranges and individual numbers
        $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
            if ($_ -match '^(\d+)-(\d+)$') {
                $start, $end = [int]$Matches[1], [int]$Matches[2]
                $selectedItems += $domainItems | Where-Object { $_.Index -ge $start -and $_.Index -le $end }
            } elseif ($_ -match '^\d+$') {
                $n = [int]$_
                $selectedItems += $domainItems | Where-Object { $_.Index -eq $n }
            }
        }
    }
    $selectedItems = $selectedItems | Sort-Object Index -Unique

    if ($selectedItems.Count -eq 0) { Write-Err 'No valid selection'; return }

    Write-Warn "About to delete $($selectedItems.Count) domain(s)"
    $confirm = Read-Host "Type 'yes' to confirm"
    if ($confirm -ne 'yes') { Write-Info 'Cancelled'; return }

    # Execute deletion
    Write-Host "`n==================== Deleting ====================" -ForegroundColor Yellow
    foreach ($item in $selectedItems) {
        Write-Info "Deleting domain '$($item.DomainName)' from $($item.ProjectName) ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/domains/$($item.DomainName)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
        if ($resp -and $resp.success) { Write-Ok "  Deleted $($item.DomainName)" }
        else { Write-Err "  Failed: $($item.DomainName)" }
    }
}

function Add-CustomDomains {
    <#
    .SYNOPSIS
        Set NEW_DOMAIN on selected accounts.
    #>
    param([object[]]$Accounts)
    if (-not $Accounts) { return }

    Write-Host "`n==================== Adding custom domains ====================" -ForegroundColor Yellow

    foreach ($acct in $Accounts) {
        if (-not $acct.NewDomain) { Write-Warn "$($acct.Name): no new domain configured (set CF_X_PAGES_NEW_DOMAIN in .env)"; continue }

        Write-Info "Adding domain '$($acct.NewDomain)' to $($acct.Project) ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)/domains"
        $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.NewDomain }
        if ($resp -and $resp.success) {
            Write-Ok "$($acct.Name): domain '$($acct.NewDomain)' added (status=$($resp.result.status))"
        } else {
            $errMsg = if ($resp) { $resp.errors | ConvertTo-Json -Compress } else { 'unknown error' }
            Write-Err "$($acct.Name): add failed - $errMsg"
        }
    }
}

function Remove-Projects {
    <#
    .SYNOPSIS
        Query Cloudflare for actual projects, let user pick which to delete.
        Uses .env only for credentials.
    #>
    $accounts = Get-Accounts
    if (-not $accounts) { return }

    Write-Host "`n========== Fetching actual projects from Cloudflare ==========" -ForegroundColor Yellow

    $projectItems = @()
    $globalIdx = 0

    foreach ($acct in $accounts) {
        Write-Info "Querying $($acct.Name) ..."
        $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
        if (-not $resp -or -not $resp.success) { Write-Warn "  Skipping $($acct.Name) - API error"; continue }

        foreach ($project in $resp.result) {
            $globalIdx++
            $projectItems += [PSCustomObject]@{
                Index       = $globalIdx
                AccountName = $acct.Name
                AccountId   = $acct.AccountId
                Token       = $acct.Token
                ProjectName = $project.name
                Domains     = ($project.domains -join ', ')
            }
        }
    }

    if ($projectItems.Count -eq 0) { Write-Info 'No projects found on Cloudflare'; return }

    Write-Host "`nFound $($projectItems.Count) project(s):" -ForegroundColor Cyan
    foreach ($item in $projectItems) {
        Write-Host "  [$($item.Index)] $($item.AccountName) | $($item.ProjectName) | domains: $($item.Domains)" -ForegroundColor White
    }
    Write-Host '  [A]ll'
    Write-Host '  [Q]uit'
    Write-Host ''

    $sel = Read-Host "Enter number(s) to delete (e.g. '1,3' or '1-3')"
    if ($sel -match '^[Qq]$') { Write-Info 'Cancelled'; return }

    $selectedItems = @()
    if ($sel -match '^[Aa]$') {
        $selectedItems = $projectItems
    } else {
        $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
            if ($_ -match '^(\d+)-(\d+)$') {
                $start, $end = [int]$Matches[1], [int]$Matches[2]
                $selectedItems += $projectItems | Where-Object { $_.Index -ge $start -and $_.Index -le $end }
            } elseif ($_ -match '^\d+$') {
                $n = [int]$_
                $selectedItems += $projectItems | Where-Object { $_.Index -eq $n }
            }
        }
    }
    $selectedItems = $selectedItems | Sort-Object Index -Unique

    if ($selectedItems.Count -eq 0) { Write-Err 'No valid selection'; return }

    Write-Warn "WARNING: About to permanently delete $($selectedItems.Count) project(s) and ALL deployments!"
    Write-Warn 'This action CANNOT be undone!'
    $confirm = Read-Host "Type 'yes' to confirm"
    if ($confirm -ne 'yes') { Write-Info 'Cancelled'; return }

    Write-Host "`n==================== Deleting ====================" -ForegroundColor Yellow
    foreach ($item in $selectedItems) {
        Write-Info "Deleting project '$($item.ProjectName)' ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
        if ($resp -and $resp.success) { Write-Ok "  Deleted $($item.ProjectName)" }
        else { Write-Err "  Failed: $($item.ProjectName)" }
    }
}

function New-Projects {
    <#
    .SYNOPSIS
        Create new Pages projects for selected accounts (using NEW_PROJECT_NAME).
    #>
    param([object[]]$Accounts)
    if (-not $Accounts) { return }

    Write-Host "`n==================== Creating projects ====================" -ForegroundColor Yellow

    foreach ($acct in $Accounts) {
        $newName = if ($acct.NewProject) { $acct.NewProject } else { $acct.Project }
        Write-Info "Creating project '$newName' for $($acct.Name) ..."

        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects"
        $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $newName }
        if ($resp -and $resp.success) {
            Write-Ok "$($acct.Name): project '$newName' created"
        } else {
            $errMsg = if ($resp) { $resp.errors | ConvertTo-Json -Compress } else { 'unknown error' }
            Write-Err "$($acct.Name): create failed - $errMsg"
        }
    }
}

# ================================================================
# KV namespace helpers
# ================================================================

function Get-KvList {
    <#
    .SYNOPSIS
        List KV namespaces for an account.
    #>
    param([string]$AccountId, [string]$Token)
    $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$AccountId/storage/kv/namespaces" -Token $Token
    if ($resp -and $resp.success) { return $resp.result }
    return @()
}

function Ensure-KvNamespace {
    <#
    .SYNOPSIS
        Ensure a KV namespace exists. If namespace_id is provided and valid, return it.
        Otherwise, create a new one with the given title.
    #>
    param([string]$AccountId, [string]$Token, [string]$NamespaceId, [string]$Title)
    if ($NamespaceId) {
        $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$AccountId/storage/kv/namespaces/$NamespaceId" -Token $Token
        if ($resp -and $resp.success) { return $NamespaceId }
        Write-Warn "  KV namespace $NamespaceId not found, will create new one"
    }
    $resp = Invoke-CfApi -Method Post -Uri "https://api.cloudflare.com/client/v4/accounts/$AccountId/storage/kv/namespaces" -Token $Token -Body @{ title = $Title }
    if ($resp -and $resp.success) {
        Write-Ok "  Created KV namespace '$Title' (id=$($resp.result.id))"
        return $resp.result.id
    }
    Write-Err "  Failed to create KV namespace '$Title'"
    return $null
}

# ================================================================
# Deploy projects - create, configure, and upload
# ================================================================

function Prepare-Source {
    <#
    .SYNOPSIS
        Prepare deployment source directory.
        Downloads from URL if needed, extracts zip, returns source path.
    #>
    # Read global FILES_TO_REDEPLOY_* from .env
    $deployDir  = $null
    $downloadUrl = $null
    $envPath = Join-Path -Path $PSScriptRoot -ChildPath '.env'
    if (Test-Path -LiteralPath $envPath) {
        Get-Content -LiteralPath $envPath -Encoding UTF8 | ForEach-Object {
            $t = $_.Trim()
            if ($t -match '^FILES_TO_REDEPLOY_DIR=(.+)')         { $deployDir   = $Matches[1] }
            if ($t -match '^FILES_TO_REDEPLOY_DOWNLOAD_URL=(.+)') { $downloadUrl = $Matches[1] }
        }
    }
    if (-not $deployDir) { $deployDir = 'files-to-redeploy' }
    if (-not [System.IO.Path]::IsPathRooted($deployDir)) {
        $deployDir = Join-Path -Path $PSScriptRoot -ChildPath $deployDir
    }
    $deployDir = [System.IO.Path]::GetFullPath($deployDir)

    # Check if source dir already has files
    $extractedDir = Join-Path -Path $deployDir -ChildPath 'extracted'
    $sourceCandidates = @(Get-ChildItem -Directory -LiteralPath $extractedDir -ErrorAction SilentlyContinue)
    if ($sourceCandidates.Count -gt 0) {
        return $sourceCandidates[0].FullName
    }
    # Check deploy dir itself
    $hasFiles = @(Get-ChildItem -LiteralPath $deployDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -notin '.zip', '.hash', '.url' })
    if ($hasFiles.Count -gt 0) { return $deployDir }

    # Try to download and extract
    if (-not $downloadUrl) { Write-Err 'No source files found and FILES_TO_REDEPLOY_DOWNLOAD_URL not set'; return $null }
    $zipFile = Join-Path -Path $deployDir -ChildPath 'source.zip'
    Write-Info "Downloading source from $downloadUrl ..."
    try {
        Invoke-WebRequest -Uri $downloadUrl -OutFile $zipFile -UseBasicParsing
        $null = New-Item -ItemType Directory -Path $extractedDir -Force
        Expand-Archive -Path $zipFile -DestinationPath $extractedDir -Force
        $src = Get-ChildItem -Directory -LiteralPath $extractedDir | Select-Object -First 1 -ExpandProperty FullName
        if (-not $src) { $src = $extractedDir }
        Write-Ok "Source ready at $src"
        return $src
    } catch { Write-Err "Download/extract failed: $_"; return $null }
}

function Set-ProjectConfig {
    <#
    .SYNOPSIS
        Set env vars and KV binding on an existing Pages project via PATCH.
    #>
    param([object]$Account, [string]$ProjectName)
    $envVars = [ordered]@{}
    foreach ($vName in $Account.Vars.Keys) {
        $v = $Account.Vars[$vName]
        if ($v.value) { $envVars[$vName] = @{ value = $v.value; type = $v.type } }
    }
    if ($envVars.Count -eq 0 -and -not $Account.KvvNamespaceId) { return $true }

    $depCfg = [ordered]@{}
    $cfg = @{}
    if ($envVars.Count -gt 0) { $cfg['env_vars'] = $envVars }
    if ($Account.KvvNamespaceId) {
        $bindingName = if ($Account.KvvBinding) { $Account.KvvBinding } else { 'KV' }
        $cfg['kv_namespaces'] = @{ $bindingName = @{ namespace_id = $Account.KvvNamespaceId } }
    }
    switch -Wildcard ($Account.ProjectType) {
        'production' { $depCfg.production = $cfg }
        'preview'    { $depCfg.preview    = $cfg }
        default      { $depCfg.production = $cfg; $depCfg.preview = $cfg }
    }
    $uri = "https://api.cloudflare.com/client/v4/accounts/$($Account.AccountId)/pages/projects/$ProjectName"
    $resp = Invoke-CfApi -Method Patch -Uri $uri -Token $Account.Token -Body @{ deployment_configs = $depCfg }
    if ($resp -and $resp.success) { return $true }
    Write-Err "  Failed to set project config for $ProjectName"
    return $false
}

function Deploy-Projects {
    <#
    .SYNOPSIS
        Deploy selected accounts: create/update project, set vars, bind KV, set domain, upload source.
    #>
    $accts = Select-Accounts
    if (-not $accts) { return }

    Write-Host "`n========== Deploy Projects ==========" -ForegroundColor Magenta
    Write-Host 'This will:' -ForegroundColor White
    Write-Host '  1. Prepare deployment source files'
    Write-Host '  2. Create projects (from NEW_PROJECT_NAME or existing PROJECT_NAME)'
    Write-Host '  3. Set environment variables (UUID, ADMIN, etc.)'
    Write-Host '  4. Ensure KV namespace exists and bind to project'
    Write-Host '  5. Set custom domain (from NEW_DOMAIN)'
    Write-Host '  6. Upload source files via wrangler'
    Write-Host ''

    # Step 1: Prepare source
    Write-Host '>> Preparing source files ...' -ForegroundColor Cyan
    $sourceDir = Prepare-Source
    if (-not $sourceDir) { return }

    # Step 2-6: Process each account
    foreach ($acct in $accts) {
        $projName = if ($acct.NewProject) { $acct.NewProject } else { $acct.Project }
        Write-Host "`n--- $($acct.Name) → $projName ---" -ForegroundColor Magenta

        # Check if project exists
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$projName"
        $existing = Invoke-CfApi -Method Get -Uri $uri -Token $acct.Token

        if (-not $existing -or -not $existing.success) {
            # Need to create project first
            Write-Info "  Creating project '$projName' ..."
            $resp = Invoke-CfApi -Method Post -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token -Body @{ name = $projName }
            if (-not $resp -or -not $resp.success) { Write-Err "  Failed to create project"; continue }
            Write-Ok "  Project '$projName' created"
        } else {
            Write-Info "  Project '$projName' exists"
        }

        # Ensure KV namespace
        $kvTitle = "${projName}-kv"
        $nsId = Ensure-KvNamespace -AccountId $acct.AccountId -Token $acct.Token -NamespaceId $acct.KvvNamespaceId -Title $kvTitle
        if (-not $nsId) { Write-Warn "  Skipping KV binding"; continue }
        $acct.KvvNamespaceId = $nsId

        # Set config (env vars + KV binding)
        Write-Info '  Setting project configuration ...'
        $ok = Set-ProjectConfig -Account $acct -ProjectName $projName
        if (-not $ok) { Write-Warn '  Config may be incomplete' }

        # Set custom domain
        if ($acct.NewDomain) {
            Write-Info "  Adding domain '$($acct.NewDomain)' ..."
            $domUri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$projName/domains"
            $resp = Invoke-CfApi -Method Post -Uri $domUri -Token $acct.Token -Body @{ name = $acct.NewDomain }
            if ($resp -and $resp.success) { Write-Ok "  Domain '$($acct.NewDomain)' added" }
            else { Write-Warn "  Domain add may have failed or already exists" }
        }

        # Upload source via wrangler
        Write-Info '  Uploading source files ...'
        try {
            $raw = & wrangler pages deploy $sourceDir --project-name $projName 2>&1
            $text = $raw -join "`n"
            Write-Host $text -ForegroundColor DarkGray
            if ($text -match 'Deployment complete') { Write-Ok "  Deployed to $projName" }
            else { Write-Err "  Deploy may have failed - check output above" }
        } catch { Write-Err "  Deploy exception: $_" }
    }

    Write-Host "`n========== Deploy Complete ==========" -ForegroundColor Green
}

function Full-Workflow {
    <#
    .SYNOPSIS
        Full lifecycle: interactively delete old → create new → configure → deploy.
    #>
    Write-Host "`n=============== Full Workflow ===============" -ForegroundColor Magenta
    Write-Host 'This will walk you through:' -ForegroundColor White
    Write-Host '  Step 1 - Delete old custom domains (interactive)'
    Write-Host '  Step 2 - Delete old projects (interactive)'
    Write-Host '  Step 3 - Deploy new projects (create + config + KV + domain + upload)'
    Write-Host ''

    Write-Host "========== Step 1: Delete old domains ==========" -ForegroundColor Cyan
    Remove-CustomDomains
    Write-Host "`nPress Enter to continue to Step 2 ..." -ForegroundColor DarkGray
    try { [Console]::In.ReadLine() | Out-Null } catch { }

    Write-Host "`n========== Step 2: Delete old projects ==========" -ForegroundColor Cyan
    Remove-Projects
    Write-Host "`nPress Enter to continue to Step 3 ..." -ForegroundColor DarkGray
    try { [Console]::In.ReadLine() | Out-Null } catch { }

    Write-Host "`n========== Step 3: Deploy new projects ==========" -ForegroundColor Cyan
    Deploy-Projects

    Write-Host "`n=============== Full Workflow Complete ===============" -ForegroundColor Green
}

# ================================================================
# Entry point: main menu loop
# ================================================================
do {
    $null = try { Clear-Host } catch { }
    Write-Host '====================================================' -ForegroundColor Cyan
    Write-Host '          Cloudflare Pages Manager' -ForegroundColor Cyan
    Write-Host '====================================================' -ForegroundColor Cyan
    Write-Host '  1.  Sync .env with Cloudflare state'
    Write-Host '  2.  Delete custom domain(s)          (fetches real-time from CF)'
    Write-Host '  3.  Add custom domain(s)              (from .env NEW_DOMAIN)'
    Write-Host '  4.  Delete project(s)                 (fetches real-time from CF)'
    Write-Host '  5.  Create project(s)                 (from .env NEW_PROJECT_NAME)'
    Write-Host '  6.  Deploy project(s)                 (create + config + KV + domain + upload)'
    Write-Host '  7.  Full workflow                     (delete old → deploy new)'
    Write-Host '  Q.  Quit'
    Write-Host '====================================================' -ForegroundColor Cyan

    $choice = Read-Host 'Choice'

    switch -Regex ($choice) {
        '^[Qq]$'       { break }
        '^1$'          {
            Sync-EnvState
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^2$'          {
            Remove-CustomDomains
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^3$'          {
            $accts = Select-Accounts
            if ($accts) { Add-CustomDomains -Accounts $accts }
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^4$'          {
            Remove-Projects
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^5$'          {
            $accts = Select-Accounts
            if ($accts) { New-Projects -Accounts $accts }
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^6$'          {
            Deploy-Projects
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^7$'          {
            Full-Workflow
            Write-Host "`nPress Enter to continue ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        default        {
            Write-Warn 'Invalid choice'
            Start-Sleep -Seconds 1
        }
    }
} while ($choice -notmatch '^[Qq]$')

# Cleanup env vars
Remove-Item -LiteralPath Env:\CLOUDFLARE_API_TOKEN  -ErrorAction SilentlyContinue
Remove-Item -LiteralPath Env:\CLOUDFLARE_ACCOUNT_ID -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Press Enter to exit ...' -ForegroundColor DarkGray
try { [Console]::In.ReadLine() | Out-Null } catch { Start-Sleep -Seconds 3 }
exit 0
