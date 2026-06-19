---
slug: refactor-deploy
status: drafting
intent: clear
pending-action: write .omo/plans/refactor-deploy.md
approach: Refactor deploy.ps1 based on Cloudflare Pages skill best practices — restructure menu to two primary workflows (Delete + Deploy), add KV namespace deletion capability, handle 100+ deployments edge case for project deletion, improve error handling with retry, add CNAME prerequisite warning.
status: awaiting-approval
---

# Draft: refactor-deploy

## Components (topology ledger)
<!-- Lock the SHAPE before depth. One row per top-level component that can succeed or fail independently. -->
| id | outcome (one line) | status | evidence path |
|---|---|---|---|
| F1 | Refactored deploy.ps1 with new menu structure | active | deploy.ps1 |
| F2 | Delete Workflow: query CF actual state → batch delete domains+projects+KV | active | deploy.ps1#Remove-CustomDomains, Remove-Projects, Remove-KvNamespaces |
| F3 | Deploy Workflow: prepare source → create/update project → KV → env vars → domain → wrangler upload | active | deploy.ps1#Deploy-Projects |
| F4 | KV namespace manager: list, create, delete from CF | active | deploy.ps1#Remove-KvNamespaces |
| F5 | 100+ deployments edge case: cleanup before project deletion | active | deploy.ps1#Remove-Projects |
| F6 | Better error handling: Invoke-CfApi with retry logic | active | deploy.ps1#Invoke-CfApi |

## Open assumptions (announced defaults)
| assumption | adopted default | rationale | reversible? |
|---|---|---|---|
| Menu should remain interactive-only | Per user: "仅保留交互菜单" | User prefers interactive mode | Yes |
| KV namespace deletion requires user selection | Per user: "万一有多个KV呢" — list all KV namespaces per account, user picks which to delete | User needs visibility | Yes |
| Source deployment keeps download+extract flow | Per user: "保留下载+解压+上传" | Existing workflow works | Yes |
| Delete order = per-account: for-each-project {ask → delete domain → delete project} → then select+delete KV | Per user: 逐项目问"是否删除" + KV也要选 | User control + safety | Yes |
| 100+ deployments should be cleaned before project deletion | CF API requirement: project with 100+ deployments cannot be deleted directly | Known Pages issue | Yes |
| Deploy flow: fully automatic after account selection | Per user: "全部自动部署" | User preference | Yes |

## Findings (cited - path:lines)
- Current menu: 7 options + main loop (deploy.ps1:650-711)
- Get-Accounts parses .env with prefix-based parsing (deploy.ps1:38-100)
- Remove-CustomDomains: queries from CF, interactive selection, batch delete (deploy.ps1:214-297)
- Remove-Projects: queries from CF, interactive selection, batch delete (deploy.ps1:324-400)
- KV deletion: currently MISSING — no function exists
- Set-ProjectConfig: PATCHs deployment_configs with env_vars + kv_namespaces (deploy.ps1:513-543)
- Deploy-Projects: full flow — prepare source, create/update, KV, config, domain, wrangler upload (deploy.ps1:545-617)
- .env: 4 active accounts (B=cfc1, C=cfc2, D=cfc3), 2 commented out (A, E)
- KV_NAMESPACE_ID values are project names, not actual KV IDs — needs CF query to resolve
- .wrangler/cache/pages.json references old account/project (legacy)
- Pages skill: Delete project with 100+ deployments requires individual deployment cleanup first
- Pages skill: "To delete a custom domain: remove the CNAME record first"
- KV namespace delete API: DELETE /accounts/{account_id}/storage/kv/namespaces/{namespace_id}
- Deployment list API: GET /accounts/{account_id}/pages/projects/{project_name}/deployments

## Decisions (with rationale)
1. **Menu only 2 items**: Delete Workflow (option 1) and Deploy Workflow (option 2). Rationale: User's explicit requirement — only these two operations.
2. **Delete Workflow = list projects → select which to delete**:
   - Select accounts
   - For each account: list CF projects → user picks which to delete (by index/A/skip)
   - Delete selected: auto-delete custom domains → cleanup deployments → delete project
   - After all projects processed: list KV namespaces → user selects which to delete
   - Rationale: User needs visibility into multiple projects, efficient batch selection rather than per-project y/N
3. **Deploy Workflow = fully automatic** after account selection. Rationale: User confirmed "全部自动部署".
4. **Add Remove-KvNamespaces function**: Query all KV namespaces from CF, show bound project info, let user select to delete. Rationale: Currently missing capability, user needs it.
5. **Add Get-ProjectDeployments + Remove-ProjectDeployments**: Before project deletion, clean up deployments if > 100. Rationale: Known Pages limitation.
6. **Add retry logic to Invoke-CfApi**: Retry on transient failures (3 attempts, 2s delay). Rationale: Network/Cf API may have transient issues.
7. **Add CNAME prerequisite warning in Remove-CustomDomains**: Warn user to remove DNS CNAME before deleting domain. Rationale: Pages skill explicitly states this requirement.
8. **Keep all existing functions but hide from menu**: Sync-EnvState, New-Projects, Add-CustomDomains, Get-KvList, Ensure-KvNamespace remain callable internally. Rationale: Not removed, just not exposed as menu items.

## Scope IN
- New menu: only 2 items (Delete Workflow, Deploy Workflow)
- Delete Workflow: select accounts → delete custom domains → delete projects (with deployment cleanup) → optionally delete KV namespaces
- Deploy Workflow: same as current Deploy-Projects (prepare source → create/update → KV → env → domain → wrangler upload)
- Add Remove-KvNamespaces function (query + select + delete)
- Add deployment cleanup before project deletion (for 100+ deployments)
- Add Invoke-CfApi retry logic
- Add CNAME warning to domain deletion
- All existing functions remain (in script, not in menu)

## Scope OUT (Must NOT have)
- Non-interactive / CLI parameter mode (user declined)
- No secondary menu items (Sync, Add Domain, Create Project, KV Manager)
- Remove or rewrite working functions (they stay in script, just not in menu)
- Change .env parsing logic or format
- Modify the edgetunnel source files
- Change wrangler CLI usage pattern
- Add new external dependencies

## Open questions
None — all user preferences clarified.

## Approval gate
status: awaiting-approval
