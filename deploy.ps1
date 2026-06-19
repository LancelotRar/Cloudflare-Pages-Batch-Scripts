<#
.SYNOPSIS
    Batch update Cloudflare Pages env vars and redeploy across multiple accounts.
.DESCRIPTION
    Reads .env to discover accounts, lets user select targets, updates plain_text
    variables via Cloudflare API, then redeploys from cached zip source.
.PARAMETER Selection
    Account number(s) or 'A' for all. Omit for interactive menu.
.EXAMPLE
    .\deploy.ps1
    .\deploy.ps1 -Selection 2
    .\deploy.ps1 -Selection "1,3"
    .\deploy.ps1 -Selection A
#>
[CmdletBinding()]
param([string]$Selection)

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

function Main {
    # ============================================================
    # 1.  Parse .env into structured account objects
    # ============================================================
    $envPath = Join-Path -Path $PSScriptRoot -ChildPath '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Write-Err '.env not found'; return 1
    }

    $lines       = Get-Content -LiteralPath $envPath -Encoding UTF8
    $rawAccounts = [ordered]@{}
    $currentKey  = $null

    :envline foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed -match '^#') { continue }

        # Detect start of a new account block: CF_X_NAME=email
        if ($trimmed -match '^(CF_[^_]+)_NAME=(.+)') {
            $currentKey = $Matches[1]
            $rawAccounts[$currentKey] = @{
                Id           = $currentKey
                Name         = $Matches[2]
                Token        = $null
                AccountId    = $null
                Project      = $null
                ProjectType  = 'production'
                CurrentDomain = $null
                NewProject    = ''
                NewDomain     = ''
                Vars         = [ordered]@{}
            }
            continue
        }
        if (-not $currentKey) { continue }

        # Known scalar fields (continue envline to skip later _TYPE regex)
        switch -Regex ($trimmed) {
            "^${currentKey}_TOKEN=(.*)"               { $rawAccounts[$currentKey].Token         = $Matches[1]; continue envline }
            "^${currentKey}_ACCOUNT_ID=(.*)"           { $rawAccounts[$currentKey].AccountId     = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_PROJECT_NAME=(.*)"   { $rawAccounts[$currentKey].Project       = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_PROJECT_TYPE=(.*)"   { $rawAccounts[$currentKey].ProjectType    = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_CURRENT_DOMAIN=(.*)" { $rawAccounts[$currentKey].CurrentDomain   = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_NEW_PROJECT_NAME=(.*)" { $rawAccounts[$currentKey].NewProject   = $Matches[1]; continue envline }
            "^${currentKey}_PAGES_NEW_DOMAIN=(.*)"     { $rawAccounts[$currentKey].NewDomain      = $Matches[1]; continue envline }
        }

        # Dynamic variable: CF_X_{NAME}_TYPE=plain_text  →  discovers $NAME
        if ($trimmed -match "^${currentKey}_(.+)_TYPE=(.*)") {
            $rawAccounts[$currentKey].Vars[$Matches[1]] = @{ type = $Matches[2]; value = $null }
            continue
        }

        # Dynamic variable: CF_X_{NAME}=value  →  fills value for discovered $NAME
        foreach ($known in $rawAccounts[$currentKey].Vars.Keys) {
            if ($trimmed -match "^${currentKey}_${known}=(.*)") {
                $rawAccounts[$currentKey].Vars[$known].value = $Matches[1]
                break
            }
        }
    }

    # ---- Filter to only complete accounts ----
    $accounts = $rawAccounts.Values |
        Where-Object { $_.Token -and $_.AccountId -and $_.Project } |
        Sort-Object Name

    if ($accounts.Count -eq 0) { Write-Err 'No valid accounts found'; return 1 }

    # ============================================================
    # 2.  Interactive account selection
    # ============================================================
    $null = try { Clear-Host } catch { }
    Write-Host '===================== Account list =====================' -ForegroundColor Yellow
    for ($i = 0; $i -lt $accounts.Count; $i++) {
        $a    = $accounts[$i]
        $vars = ($a.Vars.Keys | ForEach-Object { "$_ = $($a.Vars[$_].value)" }) -join ', '
        Write-Host "  [$($i+1)] $($a.Name)  ->  $($a.Project)"
        $pad = ' ' * ("  [$($i+1)] ".Length)
        if ($vars) { Write-Host "${pad}$vars" -ForegroundColor DarkGray }
    }
    Write-Host '========================================================' -ForegroundColor Yellow
    Write-Host '  [A]ll accounts  (count: ' $accounts.Count ')'
    Write-Host '  [Q]uit'
    Write-Host ''

    $sel = $Selection
    if (-not $sel) {
        try { if ([Console]::IsInputRedirected) { $sel = [Console]::In.ReadLine() } } catch { }
        if (-not $sel) { $sel = Read-Host 'Selection' }
    }

    switch -Regex ($sel) {
        '^[Qq]$' { Write-Info 'Quit'; return 0 }
        '^[Aa]$' { $targets = $accounts }
        default  {
            $targets = @()
            $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
                $n = [int]$_
                if ($n -ge 1 -and $n -le $accounts.Count) { $targets += $accounts[$n - 1] }
                else { Write-Warn "Skipping invalid index: $_" }
            }
        }
    }
    if ($targets.Count -eq 0) { Write-Err 'No valid account selected'; return 1 }

    Write-Info "Selected $($targets.Count) account(s)"

    # ============================================================
    # 3.  Prepare deployment source (cached extraction)
    # ============================================================
    # Resolve deploy dir from .env, fallback to default
    $deployDir = if ($script:filesToRedeployDir) {
        $d = $script:filesToRedeployDir
        if (-not [System.IO.Path]::IsPathRooted($d)) {
            $d = Join-Path -Path $PSScriptRoot -ChildPath $d
        }
        $d
    } else {
        Join-Path -Path $PSScriptRoot -ChildPath 'files-to-redeploy'
    }
    $deployDir = [System.IO.Path]::GetFullPath($deployDir)
    $null = New-Item -ItemType Directory -Path $deployDir -Force

    $zipFile  = Join-Path -Path $deployDir -ChildPath 'source.zip'
    $cacheDir = Join-Path -Path $deployDir -ChildPath 'extracted'
    $hashFile = Join-Path -Path $deployDir -ChildPath '.zip_hash'
    $urlFile  = Join-Path -Path $deployDir -ChildPath '.zip_url'

    # Re-download if URL changed
    if ($script:filesToRedeployUrl -and (Test-Path -LiteralPath $zipFile)) {
        $prevUrl = if (Test-Path -LiteralPath $urlFile) {
            (Get-Content -LiteralPath $urlFile -Raw -Encoding UTF8).Trim()
        } else { '' }
        if ($prevUrl -ne $script:filesToRedeployUrl) {
            Write-Info 'Download URL changed, removing old zip ...'
            Remove-Item -LiteralPath $zipFile -Force
            Remove-Item -LiteralPath $hashFile -Force -ErrorAction SilentlyContinue
        }
    }

    # Download if missing
    if (-not (Test-Path -LiteralPath $zipFile)) {
        if ($script:filesToRedeployUrl) {
            Write-Info "Downloading from $($script:filesToRedeployUrl) ..."
            Invoke-WebRequest -Uri $script:filesToRedeployUrl -OutFile $zipFile -UseBasicParsing
            Set-Content -Path $urlFile -Value $script:filesToRedeployUrl -NoNewline -Encoding UTF8
            Write-Ok 'Download complete'
        } else {
            Write-Err "Neither $zipFile exists nor FILES_TO_REDEPLOY_DOWNLOAD_URL is set"
            return 1
        }
    } else {
        Write-Info "Using local zip: $zipFile"
    }

    # Hash-based cache invalidation
    $currentHash = (Get-FileHash -Path $zipFile -Algorithm SHA256).Hash
    $cachedHash  = if (Test-Path -LiteralPath $hashFile) {
        (Get-Content -LiteralPath $hashFile -Raw -Encoding UTF8).Trim()
    } else { '' }

    if ($currentHash -ne $cachedHash) {
        Write-Info 'Zip changed, re-extracting ...'
        $null = Remove-Item -LiteralPath $cacheDir -Recurse -Force -ErrorAction SilentlyContinue
        try {
            Expand-Archive -Path $zipFile -DestinationPath $cacheDir -Force
            Set-Content -Path $hashFile -Value $currentHash -NoNewline -Encoding UTF8
            Write-Ok 'Extracted and cached'
        } catch { Write-Err "Extraction failed: $_"; return 1 }
    } else {
        Write-Info 'Zip unchanged, using cached extraction'
    }

    # Resolve source dir (archive usually contains a top-level folder)
    $sourceDir = Get-ChildItem -Directory -LiteralPath $cacheDir |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $sourceDir) { $sourceDir = $cacheDir }

    # ============================================================
    # 4.  Process each target account
    # ============================================================
    Write-Host "`n==================== Executing ====================" -ForegroundColor Yellow
    $results = @()

    :nextAccount foreach ($acct in $targets) {
        Write-Host "`n--- $($acct.Name)  ($($acct.Project)) ---" -ForegroundColor Magenta

        $env:CLOUDFLARE_API_TOKEN  = $acct.Token
        $env:CLOUDFLARE_ACCOUNT_ID = $acct.AccountId

        $accountResult = [PSCustomObject]@{
            Name    = $acct.Name
            Project = $acct.Project
            Status  = 'Failed'
            Url     = ''
            Uuid    = $acct.Vars['UUID'].value
        }

        $apiUrl = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)"

        # ---- 4a.  Update env vars via API ----
        $envVars = [ordered]@{}
        foreach ($vName in $acct.Vars.Keys) {
            $v = $acct.Vars[$vName]
            if ($v.value) { $envVars[$vName] = @{ value = $v.value; type = $v.type } }
        }

        if ($envVars.Count -gt 0) {
            Write-Info 'Updating variables ...'
            $depCfg = [ordered]@{}
            switch -Wildcard ($acct.ProjectType) {
                'production' { $depCfg.production = @{ env_vars = $envVars } }
                'preview'    { $depCfg.preview    = @{ env_vars = $envVars } }
                default      { $depCfg.production = @{ env_vars = $envVars }
                               $depCfg.preview    = @{ env_vars = $envVars } }
            }
            $body = @{ deployment_configs = $depCfg } | ConvertTo-Json -Depth 10

            try {
                $resp = Invoke-RestMethod -Uri $apiUrl -Method Patch -Headers @{
                    'Authorization' = "Bearer $($acct.Token)"
                    'Content-Type'  = 'application/json'
                } -Body $body

                if ($resp.success) { Write-Ok 'Variables updated' }
                else {
                    Write-Err "API error: $($resp.errors | ConvertTo-Json -Compress)"
                    continue nextAccount
                }
            } catch {
                Write-Err "API call failed: $_"
                continue nextAccount
            }
        }

        # ---- 4b.  Redeploy via wrangler ----
        Write-Info 'Deploying to Pages ...'
        try {
            $raw = & wrangler pages deploy $sourceDir --project-name $acct.Project 2>&1
            $text = $raw -join "`n"
            Write-Host $text -ForegroundColor DarkGray

            if ($text -match 'Deployment complete') {
                $escapedProject = [regex]::Escape($acct.Project)
                $m = [regex]::Match($text, "https://\S+\.${escapedProject}\.pages\.dev")
                $accountResult.Url   = if ($m.Success) { $m.Value } else { '' }
                $accountResult.Status = 'Success'
                Write-Ok "Deploy succeeded  $($accountResult.Url)"
            } else {
                Write-Err 'Deploy failed – check output above'
                continue nextAccount
            }
        } catch {
            Write-Err "Deploy exception: $_"
            continue nextAccount
        }

        # ---- 4c.  Verify variables ----
        Write-Info 'Verifying variables ...'
        Start-Sleep -Seconds 3
        try {
            $vr = Invoke-RestMethod -Uri $apiUrl -Method Get -Headers @{
                'Authorization' = "Bearer $($acct.Token)"
            }
            $checkEnv = if ($acct.ProjectType -eq 'preview') { 'preview' } else { 'production' }
            $deployed = $vr.result.deployment_configs.$checkEnv.env_vars
            $allOk    = $true

            foreach ($vName in $acct.Vars.Keys) {
                $v = $acct.Vars[$vName]
                if (-not $v.value) { continue }
                if ($deployed.$vName.value -eq $v.value) {
                    Write-Ok "$vName = $($v.value)  (verified)"
                } else {
                    Write-Err "$vName mismatch: expected=$($v.value), actual=$($deployed.$vName.value)"
                    $allOk = $false
                }
            }
            if ($allOk) { Write-Ok 'Verification passed' }
            else        { $accountResult.Status = 'Partial' }
        } catch {
            Write-Warn "Verification API call failed: $_"
        }

        $results += $accountResult
        Write-Ok "$($acct.Name) done"
    }

    # ============================================================
    # 5.  Summary
    # ============================================================
    Write-Host "`n====================================================" -ForegroundColor Green
    Write-Host '                     Summary' -ForegroundColor Green
    Write-Host "====================================================" -ForegroundColor Green
    foreach ($r in $results) {
        $icon   = if ($r.Status -eq 'Success') { '+' } else { 'x' }
        $colour = if ($r.Status -eq 'Success') { 'Green' } else { 'Red' }
        $statusTxt = if ($r.Status -eq 'Success') { $r.Uuid } else { $r.Status }
        Write-Host "  $icon  $($r.Name)  ->  $statusTxt" -ForegroundColor $colour
    }
    Write-Host "====================================================" -ForegroundColor Green

    return 0
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
                        if ($actualName -ne $acct.Project) {
                            Write-Warn "  Project name mismatch: .env=$($acct.Project), actual=$actualName"
                        }
                        $updatedLines += "CF_${key}_PAGES_PROJECT_NAME=$actualName"
                        $updatedLines += "CF_${key}_PAGES_CURRENT_DOMAIN=$actualDomains"
                        Write-Ok "  ${key}: project=$actualName, domain=$actualDomains"
                        $changed = $true
                        $matched = $true
                        # Skip original CURRENT_DOMAIN and NEW_* lines for this key
                        # We handle this via the next-couple-of-lines skip below
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

        # Skip CURRENT_DOMAIN and NEW_* lines - they'll be regenerated
        if ($trimmed -match '^CF_[^_]+_PAGES_CURRENT_DOMAIN=') { continue }
        if ($trimmed -match '^CF_[^_]+_PAGES_NEW_PROJECT_NAME=') { continue }
        if ($trimmed -match '^CF_[^_]+_PAGES_NEW_DOMAIN=') { continue }

        $updatedLines += $line
    }

    # Append NEW_* fields for any accounts that don't have them yet
    foreach ($acct in $accounts) {
        $hasNewProject = $envContent | Where-Object { $_ -match "^${acct.Id}_PAGES_NEW_PROJECT_NAME=" } | Select-Object -First 1
        if (-not $hasNewProject) {
            $updatedLines += "CF_$($acct.Id)_PAGES_NEW_PROJECT_NAME="
            $updatedLines += "CF_$($acct.Id)_PAGES_NEW_DOMAIN="
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

function Full-Workflow {
    <#
    .SYNOPSIS
        Full lifecycle: interactively delete old domains/projects from Cloudflare
        → create new projects (from .env NEW_PROJECT_NAME) → set new domains (from .env NEW_DOMAIN).
    #>
    Write-Host "`n=============== Full Workflow ===============" -ForegroundColor Magenta
    Write-Host 'This will walk you through the complete lifecycle:' -ForegroundColor White
    Write-Host '  Step 1 - Interactively delete OLD custom domains from Cloudflare'
    Write-Host '  Step 2 - Interactively delete OLD projects from Cloudflare'
    Write-Host '  Step 3 - Create NEW projects  (from .env NEW_PROJECT_NAME)'
    Write-Host '  Step 4 - Add NEW custom domains (from .env NEW_DOMAIN)'
    Write-Host ''

    # Step 1: Interactive domain deletion
    Write-Host "========== Step 1: Delete old domains ==========" -ForegroundColor Cyan
    Remove-CustomDomains
    Write-Host "`nPress Enter to continue to Step 2 ..." -ForegroundColor DarkGray
    try { [Console]::In.ReadLine() | Out-Null } catch { }

    # Step 2: Interactive project deletion
    Write-Host "`n========== Step 2: Delete old projects ==========" -ForegroundColor Cyan
    Remove-Projects
    Write-Host "`nPress Enter to continue to Step 3 ..." -ForegroundColor DarkGray
    try { [Console]::In.ReadLine() | Out-Null } catch { }

    # Step 3: Create new projects from .env NEW_PROJECT_NAME
    Write-Host "`n========== Step 3: Create new projects ==========" -ForegroundColor Cyan
    $accounts = Get-Accounts
    if (-not $accounts) { return }

    $toCreate = $accounts | Where-Object { $_.NewProject }
    if ($toCreate.Count -eq 0) {
        Write-Warn 'No NEW_PROJECT_NAME configured in .env. Skipping project creation.'
        Write-Warn 'Set CF_X_PAGES_NEW_PROJECT_NAME in .env and re-run.'
    } else {
        Write-Host "Will create $($toCreate.Count) new project(s):" -ForegroundColor White
        foreach ($a in $toCreate) {
            Write-Host "  $($a.Name)  →  $($a.NewProject)" -ForegroundColor Yellow
        }
        $confirm = Read-Host "Type 'yes' to create"
        if ($confirm -eq 'yes') {
            foreach ($acct in $toCreate) {
                Write-Info "Creating '$($acct.NewProject)' for $($acct.Name) ..."
                $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects"
                $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.NewProject }
                if ($resp -and $resp.success) { Write-Ok "  Created $($acct.NewProject)" }
                else { Write-Err "  Failed: $($acct.NewProject)" }
            }
        } else { Write-Info 'Skipped project creation' }
    }

    # Step 4: Add new domains
    Write-Host "`n========== Step 4: Add new custom domains ==========" -ForegroundColor Cyan
    $toDomain = $accounts | Where-Object { $_.NewDomain }
    if ($toDomain.Count -eq 0) {
        Write-Warn 'No NEW_DOMAIN configured in .env. Skipping domain setup.'
        Write-Warn 'Set CF_X_PAGES_NEW_DOMAIN in .env and re-run.'
    } else {
        Write-Host "Will add $($toDomain.Count) new domain(s):" -ForegroundColor White
        foreach ($a in $toDomain) {
            $targetProj = if ($a.NewProject) { $a.NewProject } else { $a.Project }
            Write-Host "  $($a.Name)  →  $($a.NewDomain)  (on project: $targetProj)" -ForegroundColor Yellow
        }
        $confirm = Read-Host "Type 'yes' to add domains"
        if ($confirm -eq 'yes') {
            foreach ($acct in $toDomain) {
                $targetProj = if ($acct.NewProject) { $acct.NewProject } else { $acct.Project }
                Write-Info "Adding '$($acct.NewDomain)' to $targetProj ..."
                $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$targetProj/domains"
                $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.NewDomain }
                if ($resp -and $resp.success) { Write-Ok "  Domain '$($acct.NewDomain)' set" }
                else { Write-Err "  Failed: $($acct.NewDomain)" }
            }
        } else { Write-Info 'Skipped domain setup' }
    }

    Write-Host "`n=============== Full Workflow Complete ===============" -ForegroundColor Green
    Write-Info 'You can now run option 1 (Deploy) to push your source code to the new projects.'
}

# ================================================================
# Entry point: main menu dispatch
# ================================================================
$script:filesToRedeployDir      = $null
$script:filesToRedeployUrl      = $null

# Pre-scan .env for global settings
$envPath = Join-Path -Path $PSScriptRoot -ChildPath '.env'
if (Test-Path -LiteralPath $envPath) {
    Get-Content -LiteralPath $envPath -Encoding UTF8 | ForEach-Object {
        $t = $_.Trim()
        if ($t -match '^FILES_TO_REDEPLOY_DIR=(.+)')         { $script:filesToRedeployDir = $Matches[1] }
        if ($t -match '^FILES_TO_REDEPLOY_DOWNLOAD_URL=(.+)') { $script:filesToRedeployUrl = $Matches[1] }
    }
}

# ---- Main menu loop ----
$exitCode = 0
$runOnce  = $false

# If -Selection was passed, go directly to deploy (backward compat)
if ($Selection) {
    $exitCode = Main
} else {
    do {
        $null = try { Clear-Host } catch { }
        Write-Host '====================================================' -ForegroundColor Cyan
        Write-Host '          Cloudflare Pages Manager' -ForegroundColor Cyan
        Write-Host '====================================================' -ForegroundColor Cyan
        Write-Host '  1.  Deploy project(s)            (existing workflow)'
        Write-Host '  2.  Sync .env with Cloudflare state'
        Write-Host '  3.  Delete custom domain(s)'
        Write-Host '  4.  Add custom domain(s)'
        Write-Host '  5.  Delete project(s)'
        Write-Host '  6.  Create project(s)'
        Write-Host '  7.  Full workflow (delete old → create new → set domain)'
        Write-Host '  8.  Run once: Deploy after .env sync'
        Write-Host '  Q.  Quit'
        Write-Host '====================================================' -ForegroundColor Cyan

        $choice = Read-Host 'Choice'

        switch -Regex ($choice) {
            '^[Qq]$' { $exitCode = 0; break }
            '^1$'    {
                $exitCode = Main
                $runOnce  = $true
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^2$'    {
                Sync-EnvState
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^3$'    {
                # Fetches actual domains from Cloudflare, interactive selection
                Remove-CustomDomains
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^4$'    {
                $accts = Select-Accounts
                if ($accts) { Add-CustomDomains -Accounts $accts }
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^5$'    {
                # Fetches actual projects from Cloudflare, interactive selection
                Remove-Projects
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^6$'    {
                $accts = Select-Accounts
                if ($accts) { New-Projects -Accounts $accts }
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^7$'    {
                # Full workflow - interactive delete + create new from .env config
                Full-Workflow
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            '^8$'    {
                Sync-EnvState
                $exitCode = Main
                $runOnce  = $true
                Write-Host "`nPress Enter to return to menu ..." -ForegroundColor DarkGray
                try { [Console]::In.ReadLine() | Out-Null } catch { }
            }
            default  {
                Write-Warn 'Invalid choice'
                Start-Sleep -Seconds 1
            }
        }
    } while (-not $runOnce)
}

# Cleanup
Remove-Item -LiteralPath Env:\CLOUDFLARE_API_TOKEN  -ErrorAction SilentlyContinue
Remove-Item -LiteralPath Env:\CLOUDFLARE_ACCOUNT_ID -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Press Enter to exit ...' -ForegroundColor DarkGray
try { [Console]::In.ReadLine() | Out-Null } catch { Start-Sleep -Seconds 3 }
exit $exitCode
