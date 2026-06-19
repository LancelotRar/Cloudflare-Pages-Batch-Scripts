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
    $backoff = @(2, 4, 8)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try { return Invoke-RestMethod @params }
        catch {
            $ex = $_.Exception
            $isTransient = $false
            if ($ex -is [System.Net.WebException]) {
                $statusCode = [int]$ex.Response.StatusCode
                if ($statusCode -ge 500 -or $ex.Status -eq [System.Net.WebExceptionStatus]::Timeout -or $ex.Status -eq [System.Net.WebExceptionStatus]::ConnectFailure -or $ex.Status -eq [System.Net.WebExceptionStatus]::NameResolutionFailure) {
                    $isTransient = $true
                }
            } elseif ($ex -is [System.TimeoutException] -or $ex -is [System.Net.Http.HttpRequestException]) {
                $isTransient = $true
            }
            if ($isTransient -and $attempt -lt 3) {
                Write-Warn "Retry $attempt/3: $_"
                Start-Sleep -Seconds $backoff[$attempt - 1]
            } else {
                Write-Err "API call failed: $_"
                return $null
            }
        }
    }
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
                Domain        = ''
                KvvNamespaceId = $null
                KvvBinding    = 'KV'
                Vars          = [ordered]@{}
            }
            continue
        }
        if (-not $currentKey) { continue }

        # Check if this line sets a known field (Token, AccountId, Project, etc.)
        $isKnown = $false
        switch -Regex ($trimmed) {
            "^${currentKey}_TOKEN=(.*)"               { $rawAccounts[$currentKey].Token         = $Matches[1]; $isKnown = $true; break }
            "^${currentKey}_ACCOUNT_ID=(.*)"           { $rawAccounts[$currentKey].AccountId     = $Matches[1]; $isKnown = $true; break }
            "^${currentKey}_PAGES_PROJECT_NAME=(.*)"   { $rawAccounts[$currentKey].Project       = $Matches[1]; $isKnown = $true; break }
            "^${currentKey}_PAGES_PROJECT_TYPE=(.*)"   { $rawAccounts[$currentKey].ProjectType    = $Matches[1]; $isKnown = $true; break }
            "^${currentKey}_PAGES_DOMAIN=(.*)"         { $rawAccounts[$currentKey].Domain        = $Matches[1]; $isKnown = $true; break }
            "^${currentKey}_PAGES_KV_NAMESPACE_ID=(.*)" { $rawAccounts[$currentKey].KvvNamespaceId = $Matches[1]; $isKnown = $true; break }
            "^${currentKey}_PAGES_KV_BINDING=(.*)"     { $rawAccounts[$currentKey].KvvBinding    = $Matches[1]; $isKnown = $true; break }
        }
        if ($isKnown) { continue }

        # Dynamic Vars: lines like CF_A_UUID_TYPE=plain_text define a new var
        if ($trimmed -match "^${currentKey}_(.+)_TYPE=(.*)") {
            $rawAccounts[$currentKey].Vars[$Matches[1]] = @{ type = $Matches[2]; value = $null }
            continue
        }
        # Dynamic Vars: lines like CF_A_UUID=value fill in the value
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
        $domainInfo = if ($a.Domain) { ", domain=$($a.Domain)" } else { '' }
        Write-Host "  [$($i+1)] $($a.Name)  ->  $($a.Project)$domainInfo"
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
        Pull actual Cloudflare Pages project state into .env for reference.
        Updates PROJECT_NAME, DOMAIN, and KV info to match what's actually on CF.
    #>
    Write-Info 'Syncing .env with Cloudflare actual state ...'

    $envPath       = Join-Path -Path $PSScriptRoot -ChildPath '.env'
    $envContent    = Get-Content -LiteralPath $envPath -Encoding UTF8
    $accounts      = Get-Accounts
    if (-not $accounts) { return }

    $updatedLines  = @()
    $changed       = $false

    foreach ($line in $envContent) {
        $trimmed = $line.Trim()

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
                        # Update PROJECT_NAME and DOMAIN to actual values
                        $updatedLines += "CF_${key}_PAGES_PROJECT_NAME=$actualName"
                        $updatedLines += "CF_${key}_PAGES_DOMAIN=$actualDomains"
                        # Update KV binding info
                        if ($actualKv) {
                            $kvBinding = ($actualKv.PSObject.Properties | Select-Object -First 1)
                            if ($kvBinding) {
                                $updatedLines += "CF_${key}_PAGES_KV_NAMESPACE_ID=$($kvBinding.Value.namespace_id)"
                                $updatedLines += "CF_${key}_PAGES_KV_BINDING=$($kvBinding.Name)"
                            }
                        }
                        Write-Ok "  ${key}: project=$actualName, domain=$actualDomains"
                        $changed = $true
                        # Skip original DOMAIN and KV lines for this key
                        continue
                    }
                }
            }
            # Keep original line if API failed
            $updatedLines += $line
            continue
        }

        # Skip DOMAIN and KV_* lines - they were regenerated above
        if ($trimmed -match '^CF_[^_]+_PAGES_DOMAIN=') { continue }
        if ($trimmed -match '^CF_[^_]+_PAGES_KV_') { continue }

        $updatedLines += $line
    }

    if ($changed) {
        $updatedLines -replace "`r",'' | Set-Content -LiteralPath $envPath -Encoding UTF8 -NoNewline
        Write-Ok '.env synced with current Cloudflare state'
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
    Write-Warn "======================================================"
    Write-Warn "IMPORTANT: Before deleting custom domains"
    Write-Warn "------------------------------------------------------"
    Write-Warn "1. Remove the CNAME record from your DNS provider FIRST"
    Write-Warn "2. Then delete the domain here via CF API"
    Write-Warn "3. If you skip step 1, the domain won't actually be removable from CF"
    Write-Warn "======================================================"

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
    Write-Ok "Remember to verify DNS CNAME records are cleaned up"
}

function Add-CustomDomains {
    <#
    .SYNOPSIS
        Set DOMAIN from .env on selected accounts.
    #>
    param([object[]]$Accounts)
    if (-not $Accounts) { return }

    Write-Host "`n==================== Adding custom domains ====================" -ForegroundColor Yellow

    foreach ($acct in $Accounts) {
        if (-not $acct.Domain) { Write-Warn "$($acct.Name): no domain configured (set CF_X_PAGES_DOMAIN in .env)"; continue }

        Write-Info "Adding domain '$($acct.Domain)' to $($acct.Project) ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)/domains"
        $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.Domain }
        if ($resp -and $resp.success) {
            Write-Ok "$($acct.Name): domain '$($acct.Domain)' added (status=$($resp.result.status))"
        } else {
            $errMsg = if ($resp) { $resp.errors | ConvertTo-Json -Compress } else { 'unknown error' }
            Write-Err "$($acct.Name): add failed - $errMsg"
        }
    }
}

function Get-ProjectDeployments {
    <#
    .SYNOPSIS
        List all deployments for a Pages project.
    #>
    param([string]$AccountId, [string]$Token, [string]$ProjectName)
    $uri = "https://api.cloudflare.com/client/v4/accounts/$AccountId/pages/projects/$ProjectName/deployments"
    $resp = Invoke-CfApi -Method Get -Uri $uri -Token $Token
    if ($resp -and $resp.success) { return $resp.result }
    return @()
}

function Remove-ProjectDeployments {
    <#
    .SYNOPSIS
        Batch delete deployments for a Pages project.
        Keeps the latest deployment (CF requirement).
        Rate-limited to 10 deletions/second.
    #>
    param([string]$AccountId, [string]$Token, [string]$ProjectName, [array]$Deployments)
    if ($Deployments.Count -eq 0) { return $true }

    # Keep the latest deployment (CF requirement: cannot delete latest deployment of a branch)
    $sorted = $Deployments | Sort-Object -Property created_on -Descending
    $toDelete = $sorted[1..($sorted.Count - 1)]  # skip newest

    if ($toDelete.Count -eq 0) { Write-Info "    Only 1 deployment, skipping cleanup"; return $true }

    Write-Info "    Cleaning $($toDelete.Count) old deployment(s) ..."
    $success = $true
    $count = 0
    foreach ($dep in $toDelete) {
        $uri = "https://api.cloudflare.com/client/v4/accounts/$AccountId/pages/projects/$ProjectName/deployments/$($dep.id)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $Token
        if (-not $resp -or -not $resp.success) { Write-Warn "      Failed to delete deployment $($dep.id)"; $success = $false }
        else { $count++ }
        Start-Sleep -Milliseconds 100  # rate limit
    }
    Write-Ok "    Deleted $count deployment(s)"
    return $success
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
        Write-Info "Processing '$($item.ProjectName)' ..."

        # Check deployment count
        $deployments = Get-ProjectDeployments -AccountId $item.AccountId -Token $item.Token -ProjectName $item.ProjectName
        if ($deployments.Count -gt 50) {
            Write-Warn "  Project has $($deployments.Count) deployments"
            $clean = Read-Host "  Delete old deployments first? (required for 100+) [y/N]"
            if ($clean -match '^[Yy]$') {
                Remove-ProjectDeployments -AccountId $item.AccountId -Token $item.Token -ProjectName $item.ProjectName -Deployments $deployments
            }
        }

        Write-Info "  Deleting project '$($item.ProjectName)' ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
        if ($resp -and $resp.success) { Write-Ok "  Deleted $($item.ProjectName)" }
        else { Write-Err "  Failed: $($item.ProjectName)" }
    }
}

function Remove-KvNamespaces {
    <#
    .SYNOPSIS
        Query KV namespaces from CF, interactive selection, batch delete.
        Shows which are bound to existing Pages projects.
    #>
    $accounts = Get-Accounts
    if (-not $accounts) { return }

    Write-Host "`n========== Fetching KV namespaces from Cloudflare ==========" -ForegroundColor Yellow

    # Process one account at a time for clarity
    foreach ($acct in $accounts) {
        Write-Info "Querying KV namespaces for $($acct.Name) ..."
        $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/storage/kv/namespaces" -Token $acct.Token
        if (-not $resp -or -not $resp.success) { Write-Warn "  Skipping $($acct.Name) - API error"; continue }

        $namespaces = $resp.result
        if ($namespaces.Count -eq 0) { Write-Info "  No KV namespaces found for $($acct.Name)"; continue }

        # Also fetch Pages projects to cross-reference bindings
        $projResp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
        $boundNsIds = @()
        if ($projResp -and $projResp.success) {
            foreach ($proj in $projResp.result) {
                $kvs = $proj.deployment_configs.production.kv_namespaces
                if ($kvs) { foreach ($kv in $kvs.PSObject.Properties) { $boundNsIds += $kv.Value.namespace_id } }
                $kvs = $proj.deployment_configs.preview.kv_namespaces
                if ($kvs) { foreach ($kv in $kvs.PSObject.Properties) { $boundNsIds += $kv.Value.namespace_id } }
            }
        }

        Write-Host "`nKV namespaces for $($acct.Name):" -ForegroundColor Cyan
        $kvItems = @()
        $idx = 0
        foreach ($ns in $namespaces) {
            $idx++
            $bound = if ($ns.id -in $boundNsIds) { ' (bound to project)' } else { '' }
            $kvItems += [PSCustomObject]@{ Index = $idx; AccountId = $acct.AccountId; Token = $acct.Token; NamespaceId = $ns.id; Title = $ns.title; Bound = $bound }
            Write-Host "  [$idx] $($ns.title)$bound" -ForegroundColor White
        }
        Write-Host '  [A]ll'
        Write-Host '  [Q]uit'
        Write-Host ''

        $sel = Read-Host "Enter number(s) to delete KV namespaces (e.g. '1,3' or '1-3'), [A]ll, or Enter to skip"
        if ($sel -match '^[Qq]$' -or [string]::IsNullOrWhiteSpace($sel)) { Write-Info "  Skipped KV deletion for $($acct.Name)"; continue }

        $selectedItems = @()
        if ($sel -match '^[Aa]$') { $selectedItems = $kvItems }
        else {
            $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
                if ($_ -match '^(\d+)-(\d+)$') {
                    $start, $end = [int]$Matches[1], [int]$Matches[2]
                    $selectedItems += $kvItems | Where-Object { $_.Index -ge $start -and $_.Index -le $end }
                } elseif ($_ -match '^\d+$') {
                    $n = [int]$_
                    $selectedItems += $kvItems | Where-Object { $_.Index -eq $n }
                }
            }
        }
        $selectedItems = $selectedItems | Sort-Object Index -Unique
        if ($selectedItems.Count -eq 0) { Write-Info "  No valid selection for $($acct.Name)"; continue }

        # Check if any selected are bound to projects
        $hasBound = $selectedItems | Where-Object { $_.Bound -ne '' }
        if ($hasBound) { Write-Warn "  WARNING: Some selected namespaces are still bound to projects: $($hasBound.Title -join ', ')" }

        Write-Warn "  About to delete $($selectedItems.Count) KV namespace(s)"
        $confirm = Read-Host "Type 'yes' to confirm"
        if ($confirm -ne 'yes') { Write-Info "  Cancelled for $($acct.Name)"; continue }

        foreach ($item in $selectedItems) {
            Write-Info "  Deleting KV namespace '$($item.Title)' ..."
            $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/storage/kv/namespaces/$($item.NamespaceId)"
            $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
            if ($resp -and $resp.success) { Write-Ok "    Deleted $($item.Title)" }
            else { Write-Err "    Failed: $($item.Title)" }
        }
    }
}

function New-Projects {
    <#
    .SYNOPSIS
        Create Pages projects from .env PROJECT_NAME for selected accounts.
    #>
    param([object[]]$Accounts)
    if (-not $Accounts) { return }

    Write-Host "`n==================== Creating projects ====================" -ForegroundColor Yellow

    foreach ($acct in $Accounts) {
        Write-Info "Creating project '$($acct.Project)' for $($acct.Name) ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects"
        $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.Project }
        if ($resp -and $resp.success) {
            Write-Ok "$($acct.Name): project '$($acct.Project)' created"
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
    Write-Host '  2. Create/update projects (from .env PROJECT_NAME)'
    Write-Host '  3. Set environment variables (UUID, ADMIN, etc.)'
    Write-Host '  4. Ensure KV namespace exists and bind to project'
    Write-Host '  5. Set custom domain (from .env DOMAIN)'
    Write-Host '  6. Upload source files via wrangler'
    Write-Host ''

    # Step 1: Prepare source
    Write-Host '>> Preparing source files ...' -ForegroundColor Cyan
    $sourceDir = Prepare-Source
    if (-not $sourceDir) { return }

    # Step 2-6: Process each account
    foreach ($acct in $accts) {
        Write-Host "`n--- $($acct.Name) → $($acct.Project) ---" -ForegroundColor Magenta

        # Check if project exists
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)"
        $existing = Invoke-CfApi -Method Get -Uri $uri -Token $acct.Token

        if (-not $existing -or -not $existing.success) {
            Write-Info "  Creating project '$($acct.Project)' ..."
            $resp = Invoke-CfApi -Method Post -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token -Body @{ name = $acct.Project }
            if (-not $resp -or -not $resp.success) { Write-Err "  Failed to create project"; continue }
            Write-Ok "  Project '$($acct.Project)' created"
        } else {
            Write-Info "  Project '$($acct.Project)' exists"
        }

        # Ensure KV namespace
        $kvTitle = "$($acct.Project)-kv"
        $nsId = Ensure-KvNamespace -AccountId $acct.AccountId -Token $acct.Token -NamespaceId $acct.KvvNamespaceId -Title $kvTitle
        if (-not $nsId) { Write-Warn "  Skipping KV binding"; continue }
        $acct.KvvNamespaceId = $nsId

        # Set config (env vars + KV binding)
        Write-Info '  Setting project configuration ...'
        $ok = Set-ProjectConfig -Account $acct -ProjectName $acct.Project
        if (-not $ok) { Write-Warn '  Config may be incomplete' }

        # Set custom domain
        if ($acct.Domain) {
            Write-Info "  Adding domain '$($acct.Domain)' ..."
            $domUri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)/domains"
            $resp = Invoke-CfApi -Method Post -Uri $domUri -Token $acct.Token -Body @{ name = $acct.Domain }
            if ($resp -and $resp.success) { Write-Ok "  Domain '$($acct.Domain)' added" }
            else { Write-Warn "  Domain add may have failed or already exists" }
        }

        # Upload source via wrangler
        Write-Info '  Uploading source files ...'
        try {
            $raw = & wrangler pages deploy $sourceDir --project-name $acct.Project 2>&1
            $text = $raw -join "`n"
            Write-Host $text -ForegroundColor DarkGray
            if ($text -match 'Deployment complete') { Write-Ok "  Deployed to $($acct.Project)" }
            else { Write-Err "  Deploy may have failed - check output above" }
        } catch { Write-Err "  Deploy exception: $_" }
    }

    Write-Host "`n========== Deploy Complete ==========" -ForegroundColor Green
}

function Delete-Workflow {
    <#
    .SYNOPSIS
        Batch delete workflow: select accounts → for each: list projects → select to delete → delete domains+project → select KV to delete.
    #>
    $accts = Select-Accounts
    if (-not $accts) { return }

    Write-Host "`n========== Delete Workflow ==========" -ForegroundColor Magenta
    Write-Host 'This will walk through each account:' -ForegroundColor White
    Write-Host '  1. List projects from Cloudflare'
    Write-Host '  2. Select which projects to delete (deletes custom domains + project)'
    Write-Host '  3. Optionally delete KV namespaces'
    Write-Host ''

    foreach ($acct in $accts) {
        Write-Host "`n--- $($acct.Name) ---" -ForegroundColor Magenta

        # Query projects
        Write-Info "Querying projects ..."
        $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
        if (-not $resp -or -not $resp.success) { Write-Warn "  Skipping $($acct.Name) - API error"; continue }

        $projects = $resp.result
        if ($projects.Count -eq 0) { Write-Info "  No projects found for $($acct.Name)"; continue }

        # Display projects for this account
        Write-Host "`nProjects for $($acct.Name):" -ForegroundColor Cyan
        $projItems = @()
        $idx = 0
        foreach ($proj in $projects) {
            $idx++
            $domains = ($proj.domains | Where-Object { $_ -ne "$($proj.name).pages.dev" }) -join ', '
            $domainStr = if ($domains) { " | domains: $domains" } else { '' }
            $projItems += [PSCustomObject]@{
                Index = $idx
                ProjectName = $proj.name
                Domains = $proj.domains
                AccountId = $acct.AccountId
                Token = $acct.Token
            }
            Write-Host "  [$idx] $($proj.name)$domainStr" -ForegroundColor White
        }
        Write-Host '  [A]ll'
        Write-Host '  [Q]uit'
        Write-Host ''

        $sel = Read-Host "Enter number(s) to delete (e.g. '1,3' or '1-3'), [A]ll, or Enter to skip"
        if ($sel -match '^[Qq]$' -or [string]::IsNullOrWhiteSpace($sel)) { Write-Info "  Skipped $($acct.Name)"; continue }

        $selectedProjs = @()
        if ($sel -match '^[Aa]$') { $selectedProjs = $projItems }
        else {
            $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
                if ($_ -match '^(\d+)-(\d+)$') {
                    $start, $end = [int]$Matches[1], [int]$Matches[2]
                    $selectedProjs += $projItems | Where-Object { $_.Index -ge $start -and $_.Index -le $end }
                } elseif ($_ -match '^\d+$') {
                    $n = [int]$_
                    $selectedProjs += $projItems | Where-Object { $_.Index -eq $n }
                }
            }
        }
        $selectedProjs = $selectedProjs | Sort-Object Index -Unique
        if ($selectedProjs.Count -eq 0) { Write-Info "  No valid selection for $($acct.Name)"; continue }

        Write-Warn "  About to delete $($selectedProjs.Count) project(s) and their custom domains"
        $confirm = Read-Host "Type 'yes' to confirm"
        if ($confirm -ne 'yes') { Write-Info "  Cancelled for $($acct.Name)"; continue }

        # Delete each selected project: domains first, then project
        foreach ($item in $selectedProjs) {
            Write-Host "`n  --- $($item.ProjectName) ---" -ForegroundColor Magenta

            # Delete custom domains
            $customDomains = $item.Domains | Where-Object { $_ -ne "$($item.ProjectName).pages.dev" }
            foreach ($d in $customDomains) {
                Write-Info "  Deleting domain '$d' ..."
                $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/domains/$d"
                $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
                if ($resp -and $resp.success) { Write-Ok "    Deleted domain $d" }
                else { Write-Warn "    Domain delete may have failed: $d" }
            }

            # Check deployment count before project deletion
            $depUri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/deployments"
            $depResp = Invoke-CfApi -Method Get -Uri $depUri -Token $item.Token
            if ($depResp -and $depResp.success -and $depResp.result.Count -gt 50) {
                Write-Warn "    Project has $($depResp.result.Count) deployments"
                $clean = Read-Host "    Delete old deployments first? (required for 100+) [y/N]"
                if ($clean -match '^[Yy]$') {
                    $sorted = $depResp.result | Sort-Object -Property created_on -Descending
                    $toDelete = $sorted[1..($sorted.Count - 1)]
                    $delCount = 0
                    foreach ($dep in $toDelete) {
                        $dUri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/deployments/$($dep.id)"
                        $dResp = Invoke-CfApi -Method Delete -Uri $dUri -Token $item.Token
                        if ($dResp -and $dResp.success) { $delCount++ }
                        Start-Sleep -Milliseconds 100
                    }
                    Write-Ok "    Cleaned $delCount deployment(s)"
                }
            }

            # Delete project
            Write-Info "  Deleting project '$($item.ProjectName)' ..."
            $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)"
            $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
            if ($resp -and $resp.success) { Write-Ok "  Deleted $($item.ProjectName)" }
            else { Write-Err "  Failed: $($item.ProjectName)" }
        }

        # After projects done, offer KV namespace deletion
        Write-Host "`n--- KV Namespaces for $($acct.Name) ---" -ForegroundColor Cyan
        $kvResp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/storage/kv/namespaces" -Token $acct.Token
        if ($kvResp -and $kvResp.success -and $kvResp.result.Count -gt 0) {
            Write-Info "  Found $($kvResp.result.Count) KV namespace(s)"
            $deleteKv = Read-Host "  Delete KV namespaces? [y/N]"
            if ($deleteKv -match '^[Yy]$') {
                # Call existing Remove-KvNamespaces or inline
                Remove-KvNamespaces
            }
        } else {
            Write-Info "  No KV namespaces found"
        }
    }

    Write-Host "`n========== Delete Workflow Complete ==========" -ForegroundColor Green
}

# ================================================================
# Entry point: main menu loop
# ================================================================
do {
    $null = try { Clear-Host } catch { }
    Write-Host '====================================================' -ForegroundColor Cyan
    Write-Host '          Cloudflare Pages Manager' -ForegroundColor Cyan
    Write-Host '====================================================' -ForegroundColor Cyan
    Write-Host '  1.  批量删除    查询 CF → 删除自定义域 + 项目 + KV'
    Write-Host '  2.  批量部署    创建/更新 Pages 项目并上传源码'
    Write-Host '  Q.  退出'
    Write-Host '====================================================' -ForegroundColor Cyan

    $choice = Read-Host '请选择'

    switch -Regex ($choice) {
        '^[Qq]$'       { break }
        '^1$'          {
            Delete-Workflow
            Write-Host "`n按 Enter 返回菜单 ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        '^2$'          {
            Deploy-Projects
            Write-Host "`n按 Enter 返回菜单 ..." -ForegroundColor DarkGray
            try { [Console]::In.ReadLine() | Out-Null } catch { }
        }
        default        {
            Write-Warn '无效选择，请重新输入'
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
