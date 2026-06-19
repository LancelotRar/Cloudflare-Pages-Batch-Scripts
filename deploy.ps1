<#
.SYNOPSIS
    Multi-account Cloudflare Pages manager — two workflows.
.DESCRIPTION
    Reads .env for multi-account config, provides interactive menu for:
    1. Batch Delete: select accounts → query CF actual state → list projects →
       select which to delete → delete custom domains + project →
       optionally delete KV namespaces. Handles 100+ deployment edge case.
    2. Batch Deploy: prepare source (download/extract) → create/update project →
       bind KV → set environment variables → set custom domain → wrangler upload.
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
                Write-Warn "重试 $attempt/3：$_"
                Start-Sleep -Seconds $backoff[$attempt - 1]
            } else {
                Write-Err "API 调用失败：$_"
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

    if ($All) { Write-Info "已选中全部 $($accounts.Count) 个账号"; return $accounts }

    $null = try { Clear-Host } catch { }
    Write-Host '===================== 账号列表 =====================' -ForegroundColor Yellow
    for ($i = 0; $i -lt $accounts.Count; $i++) {
        $a    = $accounts[$i]
        $vars = ($a.Vars.Keys | ForEach-Object { "$_ = $($a.Vars[$_].value)" }) -join ', '
        $domainInfo = if ($a.Domain) { "，域名=$($a.Domain)" } else { '' }
        Write-Host "  [$($i+1)] $($a.Name)  →  $($a.Project)$domainInfo"
    }
    Write-Host '========================================================' -ForegroundColor Yellow
    Write-Host '  [A]ll 全部账号'
    Write-Host '  [Q]uit 退出'
    Write-Host ''

    $sel = Read-Host '请选择'
    switch -Regex ($sel) {
        '^[Qq]$' { return $null }
        '^[Aa]$' { return $accounts }
        default  {
            $result = @()
            $sel -split ',' | ForEach-Object { $_.Trim() } | ForEach-Object {
                $n = [int]$_
                if ($n -ge 1 -and $n -le $accounts.Count) { $result += $accounts[$n - 1] }
                else { Write-Warn "跳过无效序号：$_" }
            }
            if ($result.Count -eq 0) { Write-Err '未选择有效账号'; return $null }
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
                Write-Info "  正在检查 $key（$($acct.Name)）..."
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
                        Write-Ok "  ${key}: 项目=$actualName, 域名=$actualDomains"
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

    Write-Host "`n========== 从 Cloudflare 获取实际域名 ==========" -ForegroundColor Yellow
    Write-Warn "======================================================"
    Write-Warn "重要：删除自定义域名前注意"
    Write-Warn "------------------------------------------------------"
    Write-Warn "1. 先从 DNS 服务商处删除 CNAME 记录"
    Write-Warn "2. 再通过 CF API 删除域名"
    Write-Warn "3. 跳过步骤 1 会导致无法从 CF 删除域名"
    Write-Warn "======================================================"

    # Collect all domains across all accounts
    $domainItems = @()  # each: @{ Index, AccountName, AccountId, Token, ProjectName, DomainName }
    $globalIdx = 0

    foreach ($acct in $accounts) {
    Write-Info "正在查询 $($acct.Name) ..."
    $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
    if (-not $resp -or -not $resp.success) { Write-Warn "  跳过 $($acct.Name) - API 错误"; continue }

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

    if ($domainItems.Count -eq 0) { Write-Info 'Cloudflare 上未找到自定义域名'; return }

    # Show selection
    Write-Host "`n找到 $($domainItems.Count) 个自定义域名：" -ForegroundColor Cyan
    foreach ($item in $domainItems) {
        Write-Host "  [$($item.Index)] $($item.AccountName) | $($item.ProjectName) | $($item.DomainName)" -ForegroundColor White
    }
    Write-Host '  [A]ll 全部'
    Write-Host '  [Q]uit 退出'
    Write-Host ''

    $sel = Read-Host "输入序号删除（如 '1,3' 或 '1-3'）"
    if ($sel -match '^[Qq]$') { Write-Info '已取消'; return }

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

    Write-Warn "即将删除 $($selectedItems.Count) 个域名"
    $confirm = Read-Host "输入 'yes' 确认"
    if ($confirm -ne 'yes') { Write-Info '已取消'; return }

    # Execute deletion
    Write-Host "`n==================== 正在删除 ====================" -ForegroundColor Yellow
    foreach ($item in $selectedItems) {
        Write-Info "正在删除域名 '$($item.DomainName)'（项目：$($item.ProjectName)）..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/domains/$($item.DomainName)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
        if ($resp -and $resp.success) { Write-Ok "  已删除 $($item.DomainName)" }
        else { Write-Err "  失败：$($item.DomainName)" }
    }
    Write-Ok "记得检查 DNS CNAME 记录是否已清理"
}

function Add-CustomDomains {
    <#
    .SYNOPSIS
        Set DOMAIN from .env on selected accounts.
    #>
    param([object[]]$Accounts)
    if (-not $Accounts) { return }

    Write-Host "`n==================== 添加自定义域名 ====================" -ForegroundColor Yellow

    foreach ($acct in $Accounts) {
        if (-not $acct.Domain) { Write-Warn "$($acct.Name): 未配置域名（在 .env 中设置 CF_X_PAGES_DOMAIN）"; continue }

        Write-Info "正在为 $($acct.Project) 添加域名 '$($acct.Domain)' ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)/domains"
        $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.Domain }
        if ($resp -and $resp.success) {
            Write-Ok "$($acct.Name): 域名 '$($acct.Domain)' 已添加（状态=$($resp.result.status)）"
        } else {
            $errMsg = if ($resp) { $resp.errors | ConvertTo-Json -Compress } else { '未知错误' }
            Write-Err "$($acct.Name): 添加失败 - $errMsg"
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

    if ($toDelete.Count -eq 0) { Write-Info "    仅 1 个部署，跳过清理"; return $true }

    Write-Info "    正在清理 $($toDelete.Count) 个旧部署 ..."
    $success = $true
    $count = 0
    foreach ($dep in $toDelete) {
        $uri = "https://api.cloudflare.com/client/v4/accounts/$AccountId/pages/projects/$ProjectName/deployments/$($dep.id)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $Token
        if (-not $resp -or -not $resp.success) { Write-Warn "      删除部署 $($dep.id) 失败"; $success = $false }
        else { $count++ }
        Start-Sleep -Milliseconds 100  # rate limit
    }
    Write-Ok "    已删除 $count 个部署"
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

    Write-Host "`n========== 从 Cloudflare 获取实际项目 ==========" -ForegroundColor Yellow

    $projectItems = @()
    $globalIdx = 0

    foreach ($acct in $accounts) {
    Write-Info "正在查询 $($acct.Name) 的项目 ..."
    $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
    if (-not $resp -or -not $resp.success) { Write-Warn "  跳过 $($acct.Name) - API 错误"; continue }

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

    if ($projectItems.Count -eq 0) { Write-Info 'Cloudflare 上未找到项目'; return }

    Write-Host "`n找到 $($projectItems.Count) 个项目：" -ForegroundColor Cyan
    foreach ($item in $projectItems) {
        Write-Host "  [$($item.Index)] $($item.AccountName) | $($item.ProjectName) | 域名：$($item.Domains)" -ForegroundColor White
    }
    Write-Host '  [A]ll 全部'
    Write-Host '  [Q]uit 退出'
    Write-Host ''

    $sel = Read-Host "输入序号删除（如 '1,3' 或 '1-3'）"
    if ($sel -match '^[Qq]$') { Write-Info '已取消'; return }

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

    if ($selectedItems.Count -eq 0) { Write-Err '未选择有效项目'; return }

    Write-Warn "警告：即将永久删除 $($selectedItems.Count) 个项目及其所有部署！"
    Write-Warn '此操作不可撤销！'
    $confirm = Read-Host "输入 'yes' 确认"
    if ($confirm -ne 'yes') { Write-Info '已取消'; return }

    Write-Host "`n==================== 正在删除 ====================" -ForegroundColor Yellow
    foreach ($item in $selectedItems) {
        Write-Info "正在处理 '$($item.ProjectName)' ..."

        # Check deployment count
        $deployments = Get-ProjectDeployments -AccountId $item.AccountId -Token $item.Token -ProjectName $item.ProjectName
        if ($deployments.Count -gt 50) {
            Write-Warn "  项目有 $($deployments.Count) 个部署"
            $clean = Read-Host "  是否先删除旧部署？（超过 100 个需先清理）[y/N]"
            if ($clean -match '^[Yy]$') {
                Remove-ProjectDeployments -AccountId $item.AccountId -Token $item.Token -ProjectName $item.ProjectName -Deployments $deployments
            }
        }

        Write-Info "  正在删除项目 '$($item.ProjectName)' ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)"
        $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
        if ($resp -and $resp.success) { Write-Ok "  已删除 $($item.ProjectName)" }
        else { Write-Err "  失败：$($item.ProjectName)" }
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

    Write-Host "`n========== 从 Cloudflare 获取 KV 命名空间 ==========" -ForegroundColor Yellow

    # Process one account at a time for clarity
    foreach ($acct in $accounts) {
    Write-Info "正在查询 $($acct.Name) 的 KV 命名空间 ..."
    $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/storage/kv/namespaces" -Token $acct.Token
    if (-not $resp -or -not $resp.success) { Write-Warn "  跳过 $($acct.Name) - API 错误"; continue }

        $namespaces = $resp.result
        if ($namespaces.Count -eq 0) { Write-Info "  $($acct.Name) 未找到 KV 命名空间"; continue }

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

        Write-Host "`n$($acct.Name) 的 KV 命名空间：" -ForegroundColor Cyan
        $kvItems = @()
        $idx = 0
        foreach ($ns in $namespaces) {
            $idx++
            $bound = if ($ns.id -in $boundNsIds) { '（已绑定项目）' } else { '' }
            $kvItems += [PSCustomObject]@{ Index = $idx; AccountId = $acct.AccountId; Token = $acct.Token; NamespaceId = $ns.id; Title = $ns.title; Bound = $bound }
            Write-Host "  [$idx] $($ns.title)$bound" -ForegroundColor White
        }
        Write-Host '  [A]ll 全部'
        Write-Host '  [Q]uit 退出'
        Write-Host ''

        $sel = Read-Host "输入序号删除 KV（如 '1,3' 或 '1-3'），[A]ll 全选，回车跳过"
        if ($sel -match '^[Qq]$' -or [string]::IsNullOrWhiteSpace($sel)) { Write-Info "  跳过 $($acct.Name) 的 KV 删除"; continue }

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
        if ($selectedItems.Count -eq 0) { Write-Info "  未选择有效 KV 命名空间（$($acct.Name)）"; continue }

        # Check if any selected are bound to projects
        $hasBound = $selectedItems | Where-Object { $_.Bound -ne '' }
        if ($hasBound) { Write-Warn "  警告：选中的命名空间中部分仍绑定到项目：$($hasBound.Title -join ', ')" }

        Write-Warn "  即将删除 $($selectedItems.Count) 个 KV 命名空间"
        $confirm = Read-Host "输入 'yes' 确认"
        if ($confirm -ne 'yes') { Write-Info "  已取消 $($acct.Name) 的 KV 删除"; continue }

        foreach ($item in $selectedItems) {
            Write-Info "  正在删除 KV 命名空间 '$($item.Title)' ..."
            $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/storage/kv/namespaces/$($item.NamespaceId)"
            $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
            if ($resp -and $resp.success) { Write-Ok "    已删除 $($item.Title)" }
            else { Write-Err "    失败：$($item.Title)" }
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

    Write-Host "`n==================== 创建项目 ====================" -ForegroundColor Yellow

    foreach ($acct in $Accounts) {
        Write-Info "正在为 $($acct.Name) 创建项目 '$($acct.Project)' ..."
        $uri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects"
        $resp = Invoke-CfApi -Method Post -Uri $uri -Token $acct.Token -Body @{ name = $acct.Project }
        if ($resp -and $resp.success) {
            Write-Ok "$($acct.Name): 项目 '$($acct.Project)' 已创建"
        } else {
            $errMsg = if ($resp) { $resp.errors | ConvertTo-Json -Compress } else { '未知错误' }
            Write-Err "$($acct.Name): 创建失败 - $errMsg"
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
        Ensure a KV namespace exists by title.
        Looks up existing KV namespaces by title, or creates a new one.
        Returns the actual namespace UUID.
    #>
    param([string]$AccountId, [string]$Token, [string]$Title)
    if ($Title) {
        Write-Info "  正在查找 KV 命名空间 '$Title' ..."
        $list = Get-KvList -AccountId $AccountId -Token $Token
        $existing = $list | Where-Object { $_.title -eq $Title } | Select-Object -First 1
        if ($existing) {
            Write-Ok "  找到已有 KV 命名空间 '$Title'（ID=$($existing.id)）"
            return $existing.id
        }
        Write-Info "  KV 命名空间 '$Title' 不存在，正在创建 ..."
        $resp = Invoke-CfApi -Method Post -Uri "https://api.cloudflare.com/client/v4/accounts/$AccountId/storage/kv/namespaces" -Token $Token -Body @{ title = $Title }
        if ($resp -and $resp.success) {
            Write-Ok "  已创建 KV 命名空间 '$Title'（ID=$($resp.result.id)）"
            return $resp.result.id
        }
        Write-Err "  创建 KV 命名空间 '$Title' 失败"
        return $null
    }
    Write-Warn "  未指定 KV 命名空间标题，跳过"
    return $null
}

# ================================================================
# Deploy projects - create, configure, and upload
# ================================================================

function Prepare-Source {
    <#
    .SYNOPSIS
        Prepare deployment source directory.
        Always downloads fresh from URL (if configured), extracts zip, returns source path.
        Downloads once per batch — caller calls this once before looping accounts.
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

    if ($downloadUrl) {
        # Fresh download every time — clean old files first
        Write-Info "正在从 $downloadUrl 下载最新源码 ..."
        if (Test-Path -LiteralPath $deployDir) {
            Remove-Item -LiteralPath $deployDir -Recurse -Force
        }
        $null = New-Item -ItemType Directory -Path $deployDir -Force
        $zipFile = Join-Path -Path $deployDir -ChildPath 'source.zip'
        try {
            Invoke-WebRequest -Uri $downloadUrl -OutFile $zipFile -UseBasicParsing
            $extractedDir = Join-Path -Path $deployDir -ChildPath 'extracted'
            $null = New-Item -ItemType Directory -Path $extractedDir -Force
            Expand-Archive -Path $zipFile -DestinationPath $extractedDir -Force
            $src = Get-ChildItem -Directory -LiteralPath $extractedDir | Select-Object -First 1 -ExpandProperty FullName
            if (-not $src) { $src = $extractedDir }
            Write-Ok "源码已就绪：$src"
            return $src
        } catch { Write-Err "下载/解压失败：$_"; return $null }
    }

    # No URL configured — fall back to local files
    $extractedDir = Join-Path -Path $deployDir -ChildPath 'extracted'
    $sourceCandidates = @(Get-ChildItem -Directory -LiteralPath $extractedDir -ErrorAction SilentlyContinue)
    if ($sourceCandidates.Count -gt 0) {
        return $sourceCandidates[0].FullName
    }
    $hasFiles = @(Get-ChildItem -LiteralPath $deployDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -notin '.zip', '.hash', '.url' })
    if ($hasFiles.Count -gt 0) { return $deployDir }

    Write-Err '未找到源码文件，且未配置 FILES_TO_REDEPLOY_DOWNLOAD_URL'
    return $null
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
    Write-Err "  项目配置设置失败：$ProjectName"
    return $false
}

function Deploy-Projects {
    <#
    .SYNOPSIS
        Deploy selected accounts: double-upload workflow for Pages projects.
        First upload creates/deploys the project, config is applied, then second upload ensures config takes effect.
    #>
    $accts = Select-Accounts
    if (-not $accts) { return }

    Write-Host "`n========== 部署项目 ==========" -ForegroundColor Magenta
    Write-Host '将对每个账号依次执行：' -ForegroundColor White
    Write-Host '  1. 确保项目存在（通过 CF API 创建）'
    Write-Host '  2. 首次上传： wrangler pages deploy（部署源码）'
    Write-Host '  3. 配置项目： 创建 KV 命名空间 → 设置环境变量 + KV 绑定 → 添加自定义域名'
    Write-Host '  4. 二次上传： wrangler pages deploy（配置生效后重新部署）'
    Write-Host ''

    # Step 1: Prepare source (shared across all accounts)
    Write-Host '>> 正在准备源码文件 ...' -ForegroundColor Cyan
    $sourceDir = Prepare-Source
    if (-not $sourceDir) { return }

    foreach ($acct in $accts) {
        Write-Host "`n--- $($acct.Name) → $($acct.Project) ---" -ForegroundColor Magenta

        # ═══════════════════════════════════════════
        # STEP 0: Ensure project exists via CF API (wrangler can't create projects non-interactively)
        # ═══════════════════════════════════════════
        Write-Info "  [1/4] 检查项目 '$($acct.Project)' 是否存在 ..."
        $checkUri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)"
        $check = Invoke-CfApi -Method Get -Uri $checkUri -Token $acct.Token
        if ($check -and $check.success) {
            Write-Ok "  项目已存在"
        } else {
            Write-Info "  项目不存在，正在通过 API 创建 ..."
            $createUri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects"
            $create = Invoke-CfApi -Method Post -Uri $createUri -Token $acct.Token -Body @{ name = $acct.Project }
            if ($create -and $create.success) {
                Write-Ok "  项目已创建"
            } else {
                Write-Err "  项目创建失败"
                continue
            }
        }

        # ═══════════════════════════════════════════
        # STEP 1: First upload — deploy source (project already exists)
        # ═══════════════════════════════════════════
        Write-Info "  [2/4] 首次上传：部署源码到 '$($acct.Project)' ..."
        $firstOk = $false
        try {
            $raw = & wrangler pages deploy $sourceDir --project-name $acct.Project 2>&1
            $text = $raw -join "`n"
            Write-Host $text -ForegroundColor DarkGray
            if ($text -match 'Deployment complete' -or $text -match 'Success') {
                Write-Ok "  首次上传完成"
                $firstOk = $true
            } else {
                Write-Err "  首次上传失败，请查看上方输出"
                continue
            }
        } catch {
            Write-Err "  首次上传异常：$_"
            continue
        }

        # ═══════════════════════════════════════════
        # STEP 3: Configure — KV namespace, env vars, domain
        # ═══════════════════════════════════════════
        Write-Info "  [3/4] 正在配置项目 ..."

        # Ensure KV namespace exists (by title from .env or fallback to project name)
        $kvTitle = if ($acct.KvvNamespaceId) { $acct.KvvNamespaceId } else { "$($acct.Project)-kv" }
        $nsId = Ensure-KvNamespace -AccountId $acct.AccountId -Token $acct.Token -Title $kvTitle
        if (-not $nsId) { Write-Warn "  跳过 KV 绑定（命名空间创建失败）"; continue }
        $acct.KvvNamespaceId = $nsId

        # Set config (env vars + KV binding) via PATCH
        $ok = Set-ProjectConfig -Account $acct -ProjectName $acct.Project
        if (-not $ok) { Write-Warn '  Config may be incomplete - continuing anyway' }

        # Set custom domain
        if ($acct.Domain) {
            Write-Info "  正在添加域名 '$($acct.Domain)' ..."
            $domUri = "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects/$($acct.Project)/domains"
            $resp = Invoke-CfApi -Method Post -Uri $domUri -Token $acct.Token -Body @{ name = $acct.Domain }
            if ($resp -and $resp.success) { Write-Ok "  域名 '$($acct.Domain)' 已添加" }
            else { Write-Warn "  域名添加可能失败或已存在" }
        }

        # ═══════════════════════════════════════════
        # STEP 4: Second upload — redeploy with config applied
        # ═══════════════════════════════════════════
        Write-Info "  [4/4] 二次上传：配置生效后重新部署 ..."
        try {
            $raw = & wrangler pages deploy $sourceDir --project-name $acct.Project 2>&1
            $text = $raw -join "`n"
            Write-Host $text -ForegroundColor DarkGray
            if ($text -match 'Deployment complete' -or $text -match 'Success') {
                Write-Ok "  ✅ 项目 '$($acct.Project)' 已完全部署并配置完成"
            } else {
                Write-Err "  二次上传可能失败，请查看上方输出"
            }
        } catch {
            Write-Err "  二次上传异常：$_"
        }
    }

    Write-Host "`n========== 部署完成 ==========" -ForegroundColor Green
}

function Delete-Workflow {
    <#
    .SYNOPSIS
        Batch delete workflow: select accounts → for each: list projects → select to delete → delete domains+project → select KV to delete.
    #>
    $accts = Select-Accounts
    if (-not $accts) { return }

    Write-Host "`n========== 批量删除 ==========" -ForegroundColor Magenta
    Write-Host '将对每个账号依次执行：' -ForegroundColor White
    Write-Host '  1. 从 Cloudflare 列出项目'
    Write-Host '  2. 选择要删除的项目（同时删除自定义域名 + 项目）'
    Write-Host '  3. 可选：删除 KV 命名空间'
    Write-Host ''

    foreach ($acct in $accts) {
        Write-Host "`n--- $($acct.Name) ---" -ForegroundColor Magenta

        # Query projects
        Write-Info "正在查询项目 ..."
        $resp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/pages/projects" -Token $acct.Token
        if (-not $resp -or -not $resp.success) { Write-Warn "  跳过 $($acct.Name) - API 错误"; continue }

        $projects = $resp.result
        if ($projects.Count -eq 0) { Write-Info "  $($acct.Name) 未找到项目"; continue }

        # Display projects for this account
        Write-Host "`n$($acct.Name) 的项目：" -ForegroundColor Cyan
        $projItems = @()
        $idx = 0
        foreach ($proj in $projects) {
            $idx++
            $domains = ($proj.domains | Where-Object { $_ -ne "$($proj.name).pages.dev" }) -join ', '
            $domainStr = if ($domains) { " | 域名：$domains" } else { '' }
            $projItems += [PSCustomObject]@{
                Index = $idx
                ProjectName = $proj.name
                Domains = $proj.domains
                AccountId = $acct.AccountId
                Token = $acct.Token
            }
            Write-Host "  [$idx] $($proj.name)$domainStr" -ForegroundColor White
        }
        Write-Host '  [A]ll 全部'
        Write-Host '  [Q]uit 退出'
        Write-Host ''

        $sel = Read-Host "输入序号删除（如 '1,3' 或 '1-3'），[A]ll 全选，回车跳过"
        if ($sel -match '^[Qq]$' -or [string]::IsNullOrWhiteSpace($sel)) { Write-Info "  跳过 $($acct.Name)"; continue }

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
        if ($selectedProjs.Count -eq 0) { Write-Info "  未选择有效项目（$($acct.Name)）"; continue }

        Write-Warn "  即将删除 $($selectedProjs.Count) 个项目及其自定义域名"
        $confirm = Read-Host "输入 'yes' 确认"
        if ($confirm -ne 'yes') { Write-Info "  已取消 $($acct.Name)"; continue }

        # Delete each selected project: domains first, then project
        foreach ($item in $selectedProjs) {
            Write-Host "`n  --- $($item.ProjectName) ---" -ForegroundColor Magenta

            # Delete custom domains
            $customDomains = $item.Domains | Where-Object { $_ -ne "$($item.ProjectName).pages.dev" }
            foreach ($d in $customDomains) {
                Write-Info "  正在删除域名 '$d' ..."
                $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/domains/$d"
                $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
                if ($resp -and $resp.success) { Write-Ok "    已删除域名 $d" }
                else { Write-Warn "    域名删除可能失败：$d" }
            }

            # Check deployment count before project deletion
            $depUri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)/deployments"
            $depResp = Invoke-CfApi -Method Get -Uri $depUri -Token $item.Token
            if ($depResp -and $depResp.success -and $depResp.result.Count -gt 50) {
                Write-Warn "    项目有 $($depResp.result.Count) 个部署"
                $clean = Read-Host "    是否先删除旧部署？（超过 100 个需先清理）[y/N]"
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
                    Write-Ok "    已清理 $delCount 个部署"
                }
            }

            # Delete project
            Write-Info "  正在删除项目 '$($item.ProjectName)' ..."
            $uri = "https://api.cloudflare.com/client/v4/accounts/$($item.AccountId)/pages/projects/$($item.ProjectName)"
            $resp = Invoke-CfApi -Method Delete -Uri $uri -Token $item.Token
            if ($resp -and $resp.success) { Write-Ok "  已删除 $($item.ProjectName)" }
            else { Write-Err "  失败：$($item.ProjectName)" }
        }

        # After projects done, offer KV namespace deletion
        Write-Host "`n--- $($acct.Name) 的 KV 命名空间 ---" -ForegroundColor Cyan
        $kvResp = Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/$($acct.AccountId)/storage/kv/namespaces" -Token $acct.Token
        if ($kvResp -and $kvResp.success -and $kvResp.result.Count -gt 0) {
            Write-Info "  找到 $($kvResp.result.Count) 个 KV 命名空间"
            $deleteKv = Read-Host "  是否删除 KV 命名空间？[y/N]"
            if ($deleteKv -match '^[Yy]$') {
                # Call existing Remove-KvNamespaces or inline
                Remove-KvNamespaces
            }
        } else {
            Write-Info "  未找到 KV 命名空间"
        }
    }

    Write-Host "`n========== 删除完成 ==========" -ForegroundColor Green
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
Write-Host '按 Enter 退出 ...' -ForegroundColor DarkGray
try { [Console]::In.ReadLine() | Out-Null } catch { Start-Sleep -Seconds 3 }
exit 0
