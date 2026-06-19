# refactor-deploy - Work Plan

## TL;DR (For humans)

**What you'll get:** 一个重构后的 `deploy.ps1`，菜单精简为两个选项：① 批量删除（查询 CF 实际状态 → 依次删除自定义域 → 项目 → KV 命名空间），② 批量部署（准备源码→创建项目→绑定KV→设环境变量→设域名→wrangler上传）。

**Why this approach:** 根据 Cloudflare Pages Skill 的最佳实践——新增了 100+ 部署记录的场景处理（删除项目前先清理部署记录）、KV 命名空间增删管理、删除自定义域前的 CNAME 提醒。保留用户偏好的纯交互菜单模式。

**What it will NOT do:** 不添加命令行参数/非交互模式；不暴露二级菜单（没有单独的同步/添加域名/创建项目/KV管理等子选项）；不改写现有工作函数；不改 .env 格式；不改 edgetunnel 源码；不新增外部依赖。

**Effort:** Medium
**Risk:** Low - 大部分是增量修改（新增函数 + 菜单重组），核心逻辑不变
**Decisions to sanity-check:** ① 删除工作流三步骤顺序（域名→项目→KV）② KV 删除前检查是否仍有项目绑定

Your next move: 审阅并批准本计划，然后执行 `$start-work`。

---

> TL;DR (machine): Medium effort, Low risk — Simplify menu to 2 options (Delete/Deploy), add KV deletion + deployment cleanup + retry logic + CNAME warning. Keep interactive-only.

## Scope
### Must have
- Menu with exactly 2 options: ① Batch Delete (domains → projects → KV), ② Batch Deploy
- `Invoke-CfApi` retry logic (3 attempts, backoff)
- `Remove-KvNamespaces` function (query CF, select, batch delete)
- `Get-ProjectDeployments` + `Remove-ProjectDeployments` for 100+ edge case
- CNAME prerequisite warning in `Remove-CustomDomains`
- Delete Workflow: select accounts → remove domains → remove projects (with deployment cleanup) → offer KV namespace deletion
- Deploy Workflow: same as current Deploy-Projects
- All existing functions remain in script (but not exposed as menu items)
- Update script header documentation

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No non-interactive CLI parameters
- No external dependencies
- No changes to .env format/parsing
- No changes to edgetunnel source files
- No removal of existing working functions
- No changes to `wrangler` usage pattern

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after + manual QA
- Evidence: .omo/evidence/task-<N>-refactor-deploy.md
- Each todo's QA section specifies exact tool invocation and expected output
- Final wave: script parse check + menu rendering test

## Execution strategy
### Parallel execution waves
- Wave 1: Tasks 1-4 (independent new/modified functions — can run in parallel with care, but retry must be first)
- Wave 2: Tasks 5-7 (menu restructure, integration, cleanup — depend on Wave 1 functions existing)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1. Invoke-CfApi retry | — | 6 | 2, 3, 4 |
| 2. Remove-KvNamespaces | 1 | 5 | 1 (after merge) |
| 3. Deployment cleanup | 1 | 5 | 1 (after merge) |
| 4. CNAME warning | — | — | 1, 2, 3 |
| 5. Menu restructure | 2, 3, 4 | — | — |
| 6. Integration test | 5 | — | — |
| 7. Cleanup docs | 5 | — | 6 |

## Todos

- [ ] 1. `Invoke-CfApi` — Add retry logic (3 attempts, 2s delay, exponential backoff)
  What to do / Must NOT do:
  - Modify the existing `Invoke-CfApi` function (deploy.ps1:25-32)
  - Wrap the `Invoke-RestMethod` call in a retry loop: try 3 times with 2s/4s/8s backoff
  - Only retry on network/transient errors (5xx, timeout, connection refused) — NOT on 4xx auth errors
  - Log each retry attempt with `Write-Warn`
  - Return `$null` if all 3 attempts fail (preserve current contract)
  - Do NOT change the function signature or return type
  - Do NOT add external dependencies

  Parallelization: Wave 1 | Blocked by: — | Blocks: 6
  References: `deploy.ps1:25-32` (current Invoke-CfApi)
  Acceptance criteria: Script parses without syntax error. If API is unreachable, 3 retries are attempted before failure.
  QA scenarios:
    - Happy: `Invoke-CfApi -Method Get -Uri "https://api.cloudflare.com/client/v4/accounts/test" -Token "dummy"` returns expected response structure
    - Failure: Simulate unreachable endpoint — function returns `$null` after 3 retries
  Commit: Y | `refactor(deploy): add retry logic to Invoke-CfApi with 3-attempt backoff`

- [ ] 2. `Remove-KvNamespaces` — New function: query KV namespaces from CF, interactive select, batch delete
  What to do / Must NOT do:
  - Add a new function `Remove-KvNamespaces` (insert near existing Remove-Projects, ~line 400)
  - For each selected account (call Get-Accounts internally, show account picker), query all KV namespaces: `GET /accounts/{accountId}/storage/kv/namespaces`
  - Display each namespace with: Index, Account, Title, NamespaceId
  - Also show which projects (if any) the namespace is bound to (cross-reference with pages/projects KV bindings)
  - Support selection: individual numbers, ranges (1-5), comma-separated, [A]ll, [Q]uit
  - Require `Type 'yes' to confirm` before deletion
  - Delete selected: `DELETE /accounts/{accountId}/storage/kv/namespaces/{namespaceId}`
  - Show progress per deletion (OK/ERROR)
  - Must NOT delete KV namespaces that still have bound Pages projects (warn and skip)
  - Must NOT modify any existing function's behavior
  - Must NOT store KV list in .env (read-only query)

  Parallelization: Wave 1 | Blocked by: 1 (uses Invoke-CfApi with retry) | Blocks: 6
  References:
    - CF API List KV: `GET /accounts/{accountId}/storage/kv/namespaces`
    - CF API Delete KV: `DELETE /accounts/{accountId}/storage/kv/namespaces/{namespaceId}`
    - Existing Remove-CustomDomains (deploy.ps1:214-297) — use as pattern for interactive selection
    - Existing Remove-Projects (deploy.ps1:324-400) — use as pattern for interactive selection
    - Existing Get-KvList (deploy.ps1:429-438) — reuses this function
  Acceptance criteria: `Get-Help Remove-KvNamespaces -Full` works. Function lists real KV namespaces from CF API, allows selection, deletes with confirmation.
  QA scenarios:
    - Happy: Select 2 KV namespaces → delete → verify "Deleted" messages for each
    - Failure: Select a KV namespace that has bound Pages projects → warning shown, delete skipped
    - Edge: No KV namespaces → "No KV namespaces found" message
  Commit: Y | `feat(deploy): add Remove-KvNamespaces function for batch KV deletion`

- [ ] 3. `Get-ProjectDeployments` + deployment cleanup in `Remove-Projects` — Handle 100+ deployments edge case
  What to do / Must NOT do:
  - Add `Get-ProjectDeployments` helper function: `GET /accounts/{accountId}/pages/projects/{projectName}/deployments`
    - Accept AccountId, Token, ProjectName parameters
    - Return list of deployment objects (id, created_on, environment, etc.)
  - Add `Remove-ProjectDeployments` helper function: batch delete deployments
    - Accept AccountId, Token, ProjectName, and deployment ID list
    - Delete each: `DELETE /accounts/{accountId}/pages/projects/{projectName}/deployments/{deploymentId}`
    - Keep the latest production deployment (CF requirement: cannot delete latest deployment of a branch)
    - Rate-limit to 10 deletions/second to avoid API throttling
  - Modify `Remove-Projects` (deploy.ps1:324-400):
    - Before deleting a project, check deployment count (> 50)
    - If > 50, ask user: "Project has N deployments. Delete them first to enable project deletion? [y/N]"
    - If yes, call Remove-ProjectDeployments, then proceed to delete project
    - If no, skip that project
  - Must NOT delete any deployments without user confirmation
  - Must NOT change the project deletion API endpoint

  Parallelization: Wave 1 | Blocked by: 1 (uses Invoke-CfApi with retry) | Blocks: 6
  References:
    - CF API List Deployments: `GET /accounts/{accountId}/pages/projects/{projectName}/deployments`
    - CF API Delete Deployment: `DELETE /accounts/{accountId}/pages/projects/{projectName}/deployments/{deploymentId}`
    - Pages skill "Known Issues": "Delete project with 100+ deployments: must delete deployments individually first"
    - Remove-Projects (deploy.ps1:324-400) — existing function to modify
    - Existing `Invoke-CfApi` (deploy.ps1:25-32) — for all API calls
  Acceptance criteria: When project has > 50 deployments, user is prompted to clean up before deletion. Deployment deletion succeeds.
  QA scenarios:
    - Happy: Project with 10 deployments → no prompt, delete directly
    - Edge: Project with 60 deployments → prompt shown → user says yes → deployments deleted → project deleted
    - Edge: User declines deployment cleanup → project deletion skipped, warning shown
  Commit: Y | `feat(deploy): add deployment cleanup before project deletion for 100+ edge case`

- [ ] 4. `Remove-CustomDomains` — Add CNAME prerequisite warning
  What to do / Must NOT do:
  - Modify `Remove-CustomDomains` (deploy.ps1:214-297)
  - Before the selection display, add a prominent warning block:
    ```powershell
    Write-Warn "=== IMPORTANT: Before deleting custom domains ==="
    Write-Warn "1. Remove the CNAME record from your DNS provider FIRST"
    Write-Warn "2. Then delete the domain here via CF API"
    Write-Warn "3. If you skip step 1, the domain won't actually be removable from CF"
    Write-Warn "=================================================="
    ```
  - After deletion, show a reminder: `Write-Ok "Remember to verify DNS CNAME records are cleaned up"`
  - Must NOT change the deletion logic itself
  - Must NOT add DNS manipulation (read-only warning)
  - Must NOT change for non-custom domains (pages.dev subdomains are handled automatically)

  Parallelization: Wave 1 | Blocked by: — | Blocks: —
  References:
    - Pages skill "Custom domains": "To delete a custom domain: remove the CNAME record first, then delete from Pages settings"
    - Remove-CustomDomains (deploy.ps1:214-297) — function to modify
  Acceptance criteria: Warning appears before any domain deletion prompt.
  QA scenarios:
    - Happy: Run Remove-CustomDomains → see CNAME warning before domain list
    - Edge: No custom domains found → warning not shown (early return)
  Commit: Y | `refactor(deploy): add CNAME prerequisite warning to Remove-CustomDomains`

- [ ] 5. Restructure main menu — Simplify to exactly 2 options
  What to do / Must NOT do:
  - Modify the main menu loop (deploy.ps1:650-711)
  - New menu:
    ```
    ====================================================
              Cloudflare Pages Manager
    ====================================================
      1.  批量删除    查询 CF → 删除自定义域 + 项目 + KV
      2.  批量部署    创建/更新 Pages 项目并上传源码
      Q.  退出
    ====================================================
    ```
  - Menu mapping:
    - Option 1: Call new `Delete-Workflow` function (see below)
    - Option 2: Call existing `Deploy-Projects` function (unchanged)
    - Q: Exit
  - Add `Delete-Workflow` function:
    - Banner explaining the flow
    - Step 1: Call `Select-Accounts` to pick which accounts to operate on
    - For each selected account:
      a. Query projects from CF: `GET /accounts/{id}/pages/projects`
      b. Display all projects with index, name, and custom domains
         - Prompt: "Enter numbers of projects to delete (e.g. '1,3' or '1-3'), [A]ll, or enter to skip:"
         - Parse selection (individual numbers, ranges, A for all)
      c. For each selected project (in one batch per account):
         i.   Query custom domains from project.domains
         ii.  Show CNAME warning, delete each custom domain
         iii. Check deployment count (>50), prompt to clean deployments first
         iv.  Delete the project via API
         v.   Show OK/ERROR per project
      d. After all projects processed, query ALL KV namespaces for this account
         - Show KV list: Index | Title | NamespaceId
         - Mark which are still bound to remaining (non-deleted) projects
         - Prompt: "Enter KV numbers to delete (e.g. '1,3' or '1-3'), [A]ll, or enter to skip:"
         - Parse selection, require "yes" confirmation, delete each
         - Show progress per deletion
  - Remove old menu items 1-7 and Full-Workflow function
  - Keep existing functions (Sync-EnvState, Add-CustomDomains, New-Projects, etc.) in the script — they are called internally, just not exposed as menu items
  - Must NOT change any existing function's behavior
  - Must NOT add non-interactive CLI parameters
  - Must NOT expose secondary menu items

  Parallelization: Wave 2 | Blocked by: 2, 3, 4 (needs the new functions) | Blocks: —
  References:
    - Current menu: deploy.ps1:650-711
    - Current Full-Workflow: deploy.ps1:619-645 (remove this function)
    - Remove-CustomDomains: deploy.ps1:214-297
    - Remove-Projects: deploy.ps1:324-400
    - Deploy-Projects: deploy.ps1:545-617
    - Select-Accounts: deploy.ps1:102-143
  Acceptance criteria: Menu shows exactly 2 options + Q. Delete-Workflow goes account-by-account, project-by-project with y/N for each, then offers KV namespace selection. Deploy-Projects runs automatically.
  QA scenarios:
    - Happy: Option 1 → select 2 accounts → account1: 3 projects listed → select "1,3" → delete 2 projects + domains → show KV list → select 2 → delete → account2 starts
    - Happy: Option 1 → account has 0 projects → "No projects" → move to KV step
    - Happy: Option 1 → Enter pressed (skip projects) → move to KV
    - Happy: Option 2 → Deploy flow runs automatically without per-project prompts
    - Edge: Q exits immediately
  Commit: Y | `refactor(deploy): simplify menu to 2 options (Delete/Deploy)`

- [ ] 6. Final integration — Wire all new functions into Delete Workflow, test end-to-end
  What to do / Must NOT do:
  - Ensure `Delete-Workflow` properly calls: Select-Accounts → per-account: Remove-CustomDomains → Remove-Projects (with deployment cleanup) → optional Remove-KvNamespaces
  - Ensure the deployment cleanup (task 3) is correctly triggered inside Remove-Projects
  - Ensure `Remove-KvNamespaces` is correctly called from Delete-Workflow
  - Verify the main loop (do...while) only has 2 options + Q
  - Run a syntax check: `PowerShell -NoProfile -Command "& { . .\deploy.ps1; Write-Host 'Parse OK' }"`
  - Show all function names to verify no conflicts

  Parallelization: Wave 2 | Blocked by: 5 (menu restructured first) | Blocks: —
  References: deploy.ps1 (entire file)
  Acceptance criteria: `PowerShell -NoProfile -Command "& { . .\deploy.ps1; Write-Host 'Parse OK' }"` outputs "Parse OK" without errors.
  QA scenarios:
    - Happy: Script parses cleanly, menu renders with just 2 options + Q
    - Failure: Any syntax error → fix immediately
  Commit: Y | `refactor(deploy): final integration of Delete Workflow with KV and deployment cleanup`

- [ ] 7. Post-refactor cleanup — Remove any dead code or stale comments
  What to do / Must NOT do:
  - Check for unused variables, orphaned comments referencing old menu items
  - Update script header `.SYNOPSIS` and `.DESCRIPTION` to reflect new structure
  - Ensure UTF-8 encoding is preserved
  - Must NOT change any functional code
  - Must NOT add new features

  Parallelization: Wave 2 | Blocked by: 5 | Blocks: —
  References: deploy.ps1:1-16 (header), entire file
  Acceptance criteria: Header describes the actual new menu structure accurately.
  QA scenarios:
    - Happy: `.SYNOPSIS` mentions "Delete Workflow (domains+projects+KV)" and "Deploy Workflow"
  Commit: Y | `docs(deploy): update header and cleanup stale comments`

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. **Parse check**: `PowerShell -NoProfile -Command "& { . .\deploy.ps1; Write-Host 'Parse OK' }"` → must output "Parse OK"
- [ ] F2. **Function listing**: Verify all functions are defined: `PowerShell -NoProfile -Command "& { . .\deploy.ps1; Get-Command -CommandType Function | Select-Object Name }"` → confirm no missing functions
- [ ] F3. **Scope fidelity**: Verify against Must NOT have list — no CLI params, no external deps, no .env changes
- [ ] F4. **Code quality review**: Check for commented-out code, dead variables, inconsistent formatting

## Commit strategy
- 7 commits total, one per todo (sequential, as each todo is completed and verified)
- Commit messages follow conventional commits format: `type(scope): message`
- Commits: retry → kv-feat → deployment-cleanup → cname-warning → menu-restructure → integration → docs-cleanup
- No squashing — preserve independent change history

## Success criteria
- `deploy.ps1` has all functions working: Invoke-CfApi (with retry), Get-Accounts, Select-Accounts, Sync-EnvState, Remove-CustomDomains (with CNAME warning), Add-CustomDomains, Remove-Projects (with deployment cleanup), New-Projects, Get-KvList, Ensure-KvNamespace, Remove-KvNamespaces, Get-ProjectDeployments, Remove-ProjectDeployments, Prepare-Source, Set-ProjectConfig, Deploy-Projects, Delete-Workflow
- Menu has exactly 2 options + Q
- `Remove-KvNamespaces` queries CF, shows namespaces, allows selection, deletes with confirmation
- `Remove-Projects` handles 100+ deployments by asking to clean up first
- `Remove-CustomDomains` shows CNAME warning before domain list
- `Invoke-CfApi` retries 3 times on transient failure
- Existing workflows (Deploy, Sync, etc.) unchanged in behavior
