# 将 deploy.ps1 重构为 Python + 适配 config.yaml

## TL;DR

**目标**：将 deploy.ps1（779 行 PowerShell）完整重写为模块化 Python CLI 工具，从 config.yaml 读取配置，保持双工作流交互菜单。

**Effort**: High | **Risk**: Medium

## 新文件结构

```
cf_wrangler/
├── __init__.py
├── __main__.py        # 入口: python -m cf_wrangler
├── models.py          # 数据类: Account, PagesConfig, EnvVar
├── config.py          # 读取 config.yaml → Account 列表
├── api.py             # Cloudflare REST API 封装
├── workflows.py       # 部署/删除业务逻辑
└── ui.py              # 交互菜单 + 彩色输出
pyproject.toml         # 依赖: pyyaml, httpx, rich
```

## PS1 → Python 映射清单

| PS1 函数 | Python 模块 | 说明 |
|---|---|---|
| `Get-Accounts` | `config.py` | 读 YAML 代替 .env 前缀解析 |
| `Select-Accounts` | `ui.py` | 只列出 `enabled: true` 的账号 |
| `Invoke-CfApi` | `api.py` → `CfApiClient` 类 | httpx + 3次重试 |
| `Prepare-Source` | `workflows.py` | 从 `files_to_redeploy` 下载解压 |
| `Set-ProjectConfig` | `workflows.py` | 用 YAML 的 `env[]` 和 `pages.kv_*` 字段 |
| `Deploy-Projects` | `workflows.py` | 双上传工作流 |
| `Delete-Workflow` | `workflows.py` | 域名→项目→KV 三步骤 |
| `Remove-KvNamespaces` | `workflows.py` | KV 交互删除 |
| `Ensure-KvNamespace` | `api.py` | 查找/创建 KV |
| `Sync-EnvState` | ❌ 废弃 | 无需同步 YAML 状态 |

## 实现任务分派

### Task A: 基础文件 (pyproject.toml + models.py + config.py + __init__.py)

**pyproject.toml**:
```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "cf-wrangler"
version = "0.1.0"
description = "Multi-account Cloudflare Pages manager — deploy and delete workflows"
requires-python = ">=3.10"
dependencies = [
    "pyyaml>=6.0",
    "httpx>=0.27",
    "rich>=13.0",
]

[project.scripts]
cf-wrangler = "cf_wrangler.__main__:main"
```

**models.py**:
```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EnvVar:
    name: str
    type: str
    value: str


@dataclass
class PagesConfig:
    project_name: str
    domain: str = ""
    kv_create: bool = False
    kv_namespace: str = ""
    kv_binding: bool = False
    kv_binding_env: str = "KV"
    project_type: str = "production"


@dataclass
class Account:
    name: str
    enabled: bool
    token: str
    account_id: str
    pages: PagesConfig
    env: list[EnvVar] = field(default_factory=list)


@dataclass
class FilesToRedeploy:
    dir: str = "files-to-redeploy"
    download_url: str = ""


@dataclass
class Config:
    files_to_redeploy: FilesToRedeploy = field(default_factory=FilesToRedeploy)
    accounts: list[Account] = field(default_factory=list)
```

**config.py**:
```python
from pathlib import Path
import yaml

from .models import Config, Account, PagesConfig, EnvVar, FilesToRedeploy


def find_config() -> Path:
    """Find config.yaml relative to the script location."""
    script_dir = Path(__file__).resolve().parent.parent
    candidates = [
        script_dir / "config.yaml",
        Path.cwd() / "config.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("config.yaml not found")


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = find_config()
    
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    
    # Global settings
    fr = raw.get("files_to_redeploy", {})
    files_to_redeploy = FilesToRedeploy(
        dir=fr.get("dir", "files-to-redeploy"),
        download_url=fr.get("download_url", ""),
    )
    
    # Accounts
    accounts: list[Account] = []
    for raw_acct in raw.get("accounts", []):
        raw_pages = raw_acct.get("pages", {})
        pages = PagesConfig(
            project_name=raw_pages.get("project_name", ""),
            domain=raw_pages.get("domain", ""),
            kv_create=raw_pages.get("kv_create", False),
            kv_namespace=raw_pages.get("kv_namespace", ""),
            kv_binding=raw_pages.get("kv_binding", False),
            kv_binding_env=raw_pages.get("kv_binding_env", "KV"),
            project_type=raw_pages.get("project_type", "production"),
        )
        
        env_list: list[EnvVar] = []
        for raw_env in raw_acct.get("env", []):
            env_list.append(EnvVar(
                name=raw_env.get("name", ""),
                type=raw_env.get("type", ""),
                value=raw_env.get("value", ""),
            ))
        
        account = Account(
            name=raw_acct.get("name", ""),
            enabled=raw_acct.get("enabled", False),
            token=raw_acct.get("token", ""),
            account_id=raw_acct.get("account_id", ""),
            pages=pages,
            env=env_list,
        )
        accounts.append(account)
    
    return Config(files_to_redeploy=files_to_redeploy, accounts=accounts)


def get_enabled_accounts(cfg: Config | None = None) -> list[Account]:
    if cfg is None:
        cfg = load_config()
    return [a for a in cfg.accounts if a.enabled and a.token and a.account_id and a.pages.project_name]
```

**__init__.py**: 空文件

### Task B: api.py

CfApiClient 类封装所有 Cloudflare REST API 调用，带 3 次重试（指数退避 2/4/8 秒）。

需要的方法：
- `list_projects() → list[dict]`
- `get_project(name) → dict|None`
- `create_project(name, branch="main") → dict|None`
- `delete_project(name) → dict|None`
- `patch_project_config(name, deployment_configs) → bool`
- `list_deployments(project_name) → list[dict]`
- `delete_deployment(project_name, deployment_id) → dict|None`
- `add_domain(project_name, domain) → dict|None`
- `delete_domain(project_name, domain) → dict|None`
- `list_kv_namespaces() → list[dict]`
- `create_kv_namespace(title) → dict|None`
- `delete_kv_namespace(namespace_id) → dict|None`
- `ensure_kv_namespace(title) → str|None`

构造函数接收 `account_id` 和 `token`。

错误处理：4xx 不重试（auth 错误），5xx/网络错误重试 3 次。

### Task C: ui.py

使用 `rich` 库构建交互界面。

函数：
- `main_menu() → int` — 显示 2 个选项 + Q 退出的主菜单，返回选择
- `select_accounts(accounts) → list[Account]` — 多账号选择（逗号/范围/A/回车跳过/Q退出）
- `select_items(items, title, item_formatter) → list` — 通用选择器，支持数字/范围/A/Q
- `confirm(prompt) → bool` — yes/no 确认
- `wait_enter()` — 按回车继续
- `print_header(text)` — 彩色标题
- `print_info(text)`, `print_ok(text)`, `print_warn(text)`, `print_error(text)` — 彩色日志

使用 `Console` + `Panel` 构建美观的界面。

### Task D: workflows.py + __main__.py

**workflows.py** — 核心业务逻辑：

1. `prepare_source(cfg) → str|None` — 下载 + 解压源码
2. `set_project_config(api, account) → bool` — 设置 env vars + KV 绑定 (PATCH)
3. `deploy_projects(api, account, source_dir) → bool` — 双上传工作流
4. `deploy_workflow(cfg)` — 完整部署流程入口
5. `delete_workflow(cfg)` — 完整删除流程入口

**__main__.py** — 入口点：
```python
from cf_wrangler.ui import main_menu
from cf_wrangler.config import load_config
from cf_wrangler.workflows import deploy_workflow, delete_workflow

def main():
    cfg = load_config()
    while True:
        choice = main_menu()
        if choice == 0: break       # Q
        elif choice == 1: delete_workflow(cfg)
        elif choice == 2: deploy_workflow(cfg)

if __name__ == "__main__":
    main()
```

## 记录详细实现规格

### api.py 详细规格

```python
import httpx
import time
from typing import Any

CF_API_BASE = "https://api.cloudflare.com/client/v4"

class CfApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

class CfApiClient:
    def __init__(self, account_id: str, token: str):
        self.account_id = account_id
        self.token = token
        self._client = httpx.Client(
            base_url=CF_API_BASE,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
    
    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        backoff = [2, 4, 8]
        url = f"/accounts/{self.account_id}{path}"
        for attempt in range(3):
            try:
                resp = self._client.request(method, url, json=body)
                data = resp.json()
                if resp.status_code >= 400:
                    is_transient = resp.status_code >= 500 or resp.status_code == 429
                    if is_transient and attempt < 2:
                        time.sleep(backoff[attempt])
                        continue
                    return data  # return response even on 4xx, caller checks success
                return data
            except (httpx.TimeoutException, httpx.ConnectionError) as e:
                if attempt < 2:
                    time.sleep(backoff[attempt])
                    continue
                return None
        return None
    
    def _paginated_get(self, path: str) -> list[dict]:
        results = []
        page = 1
        while True:
            data = self._request("GET", f"{path}?page={page}&per_page=50")
            if not data or not data.get("success"):
                break
            results.extend(data.get("result", []))
            if page >= data.get("result_info", {}).get("total_pages", 1):
                break
            page += 1
        return results
    
    def list_projects(self) -> list[dict]:
        return self._paginated_get("/pages/projects")
    
    def get_project(self, name: str) -> dict | None:
        data = self._request("GET", f"/pages/projects/{name}")
        if data and data.get("success"):
            return data["result"]
        return None
    
    def create_project(self, name: str, branch: str = "main") -> dict | None:
        return self._request("POST", "/pages/projects", {"name": name, "production_branch": branch})
    
    def delete_project(self, name: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{name}")
    
    def patch_project_config(self, name: str, deployment_configs: dict) -> bool:
        data = self._request("PATCH", f"/pages/projects/{name}", {"deployment_configs": deployment_configs})
        return data is not None and data.get("success", False)
    
    def list_deployments(self, project_name: str) -> list[dict]:
        return self._paginated_get(f"/pages/projects/{project_name}/deployments")
    
    def delete_deployment(self, project_name: str, deployment_id: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{project_name}/deployments/{deployment_id}")
    
    def add_domain(self, project_name: str, domain: str) -> dict | None:
        return self._request("POST", f"/pages/projects/{project_name}/domains", {"name": domain})
    
    def delete_domain(self, project_name: str, domain: str) -> dict | None:
        return self._request("DELETE", f"/pages/projects/{project_name}/domains/{domain}")
    
    def list_kv_namespaces(self) -> list[dict]:
        return self._paginated_get("/storage/kv/namespaces")
    
    def create_kv_namespace(self, title: str) -> dict | None:
        return self._request("POST", "/storage/kv/namespaces", {"title": title})
    
    def delete_kv_namespace(self, namespace_id: str) -> dict | None:
        return self._request("DELETE", f"/storage/kv/namespaces/{namespace_id}")
    
    def ensure_kv_namespace(self, title: str) -> str | None:
        if not title:
            return None
        namespaces = self.list_kv_namespaces()
        for ns in namespaces:
            if ns.get("title") == title:
                return ns.get("id")
        result = self.create_kv_namespace(title)
        if result and result.get("success"):
            return result["result"].get("id")
        return None
```

### workflows.py 详细规格

```python
import subprocess
import shutil
from pathlib import Path
import httpx
import zipfile
import io
import time

from .config import load_config, get_enabled_accounts
from .api import CfApiClient
from .ui import (
    print_info, print_ok, print_warn, print_error,
    select_accounts, select_items, confirm, wait_enter, print_header
)
from .models import Config, Account


def prepare_source(cfg: Config) -> Path | None:
    """Download and extract source from files_to_redeploy URL."""
    fr = cfg.files_to_redeploy
    deploy_dir = Path(fr.dir)
    if not deploy_dir.is_absolute():
        deploy_dir = Path(__file__).resolve().parent.parent / fr.dir
    deploy_dir = deploy_dir.resolve()
    
    if not fr.download_url:
        print_error("未配置 files_to_redeploy.download_url")
        return None
    
    print_info(f"正在从 {fr.download_url} 下载最新源码 ...")
    if deploy_dir.exists():
        shutil.rmtree(deploy_dir)
    deploy_dir.mkdir(parents=True)
    
    try:
        resp = httpx.get(fr.download_url, timeout=300, follow_redirects=True)
        resp.raise_for_status()
        
        zip_path = deploy_dir / "source.zip"
        zip_path.write_bytes(resp.content)
        
        extracted = deploy_dir / "extracted"
        extracted.mkdir()
        
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)
        
        # Find the first subdirectory (zip root)
        dirs = [d for d in extracted.iterdir() if d.is_dir()]
        src = dirs[0] if dirs else extracted
        
        print_ok(f"源码已就绪：{src}")
        return src
    except Exception as e:
        print_error(f"下载/解压失败：{e}")
        return None


def set_project_config(api: CfApiClient, account: Account) -> bool:
    """Set environment variables and KV binding on a Pages project."""
    env_vars = {}
    for ev in account.env:
        if ev.value:
            env_vars[ev.name] = {"value": ev.value, "type": ev.type}
    
    cfg = {}
    if env_vars:
        cfg["env_vars"] = env_vars
    if account.pages.kv_binding and account.pages.kv_namespace:
        # Need the actual namespace ID
        ns_id = api.ensure_kv_namespace(account.pages.kv_namespace)
        if ns_id:
            cfg["kv_namespaces"] = {account.pages.kv_binding_env: {"namespace_id": ns_id}}
    
    if not env_vars and not cfg.get("kv_namespaces"):
        return True
    
    dep_cfg = {}
    pt = account.pages.project_type
    if pt == "production":
        dep_cfg["production"] = cfg
    elif pt == "preview":
        dep_cfg["preview"] = cfg
    else:
        dep_cfg["production"] = cfg
        dep_cfg["preview"] = cfg
    
    return api.patch_project_config(account.pages.project_name, dep_cfg)


def deploy_project(api: CfApiClient, account: Account, source_dir: Path) -> bool:
    """Full deploy flow for a single account."""
    project = account.pages.project_name
    print_header(f"部署：{account.name} → {project}")
    
    # Step 0: Ensure project exists
    print_info(f"[1/4] 检查项目 '{project}' 是否存在 ...")
    existing = api.get_project(project)
    if existing:
        print_ok("  项目已存在")
    else:
        print_info("  项目不存在，正在通过 API 创建 ...")
        result = api.create_project(project)
        if result and result.get("success"):
            print_ok("  项目已创建")
        else:
            print_error("  项目创建失败")
            return False
    
    # Set env vars for wrangler
    env = {"CLOUDFLARE_API_TOKEN": account.token, "CLOUDFLARE_ACCOUNT_ID": account.account_id}
    
    # Step 1: First upload
    print_info(f"[2/4] 首次上传：部署源码到 '{project}' ...")
    try:
        result = subprocess.run(
            ["wrangler", "pages", "deploy", str(source_dir),
             "--project-name", project, "--branch", "main"],
            capture_output=True, text=True, env={**__import__('os').environ, **env},
            timeout=300,
        )
        output = result.stdout + result.stderr
        print(output, end="")  # Show wrangler output
        
        if "Deployment complete" not in output and "Success" not in output:
            print_error("  首次上传失败，请查看上方输出")
            return False
        print_ok("  首次上传完成")
    except subprocess.TimeoutExpired:
        print_error("  首次上传超时")
        return False
    except FileNotFoundError:
        print_error("  未找到 wrangler CLI，请先安装：npm install -g wrangler")
        return False
    
    # Step 2: Configure (KV namespace, env vars, domain)
    print_info(f"[3/4] 正在配置项目 ...")
    
    # KV namespace
    if account.pages.kv_create and account.pages.kv_namespace:
        ns_id = api.ensure_kv_namespace(account.pages.kv_namespace)
        if ns_id:
            print_ok(f"  KV 命名空间已就绪")
        else:
            print_warn("  KV 命名空间创建失败")
    
    # Set config (env vars + KV binding)
    set_project_config(api, account)
    
    # Custom domain
    if account.pages.domain:
        print_info(f"  正在添加域名 '{account.pages.domain}' ...")
        result = api.add_domain(project, account.pages.domain)
        if result and result.get("success"):
            print_ok(f"  域名 '{account.pages.domain}' 已添加")
        else:
            print_warn("  域名添加可能失败或已存在")
    
    # Step 3: Second upload
    print_info(f"[4/4] 二次上传：配置生效后重新部署 ...")
    try:
        result = subprocess.run(
            ["wrangler", "pages", "deploy", str(source_dir),
             "--project-name", project, "--branch", "main"],
            capture_output=True, text=True, env={**__import__('os').environ, **env},
            timeout=300,
        )
        output = result.stdout + result.stderr
        print(output, end="")
        
        if "Deployment complete" in output or "Success" in output:
            print_ok(f"  ✅ 项目 '{project}' 已完全部署并配置完成")
            return True
        else:
            print_error("  二次上传可能失败，请查看上方输出")
            return False
    except subprocess.TimeoutExpired:
        print_error("  二次上传超时")
        return False
    except FileNotFoundError:
        print_error("  未找到 wrangler CLI")
        return False


def deploy_workflow(cfg: Config):
    """完整部署流程入口"""
    accounts = get_enabled_accounts(cfg)
    if not accounts:
        print_error("没有已启用的账号")
        wait_enter()
        return
    
    selected = select_accounts(accounts)
    if not selected:
        return
    
    print_info("将对每个账号依次执行：")
    print_info("  1. 确保项目存在")
    print_info("  2. 首次上传：wrangler pages deploy")
    print_info("  3. 配置项目：KV 命名空间 → 环境变量 → 自定义域名")
    print_info("  4. 二次上传：配置生效后重新部署")
    
    source_dir = prepare_source(cfg)
    if not source_dir:
        wait_enter()
        return
    
    # wrangler needs to find the source path as relative or absolute
    source_dir = source_dir.resolve()
    
    for account in selected:
        api = CfApiClient(account.account_id, account.token)
        deploy_project(api, account, source_dir)
    
    print_ok("========== 部署完成 ==========")
    wait_enter()


def delete_workflow(cfg: Config):
    """完整删除流程入口"""
    accounts = get_enabled_accounts(cfg)
    if not accounts:
        print_error("没有已启用的账号")
        wait_enter()
        return
    
    selected = select_accounts(accounts)
    if not selected:
        return
    
    print_info("将对每个账号依次执行：")
    print_info("  1. 从 Cloudflare 列出项目")
    print_info("  2. 选择要删除的项目（同时删除自定义域名 + 项目）")
    print_info("  3. 可选：删除 KV 命名空间")
    
    for account in selected:
        api = CfApiClient(account.account_id, account.token)
        print_header(f"--- {account.name} ---")
        
        # Query projects
        print_info("正在查询项目 ...")
        projects = api.list_projects()
        if not projects:
            print_info(f"  {account.name} 未找到项目")
            continue
        
        # Display projects
        proj_items = []
        for i, proj in enumerate(projects, 1):
            name = proj.get("name", "")
            domains = [d for d in proj.get("domains", []) if d != f"{name}.pages.dev"]
            domain_str = f" | 域名：{', '.join(domains)}" if domains else ""
            proj_items.append({"index": i, "name": name, "project": proj, "domains": domains})
            print(f"  [{i}] {name}{domain_str}")
        
        print("  [A]ll 全部")
        print("  [Q]uit 退出")
        
        sel = input("输入序号删除（如 '1,3' 或 '1-3'），[A]ll 全选，回车跳过: ").strip()
        if not sel or sel.lower() == 'q':
            print_info(f"  跳过 {account.name}")
            continue
        
        selected_projs = parse_selection(sel, proj_items)
        if not selected_projs:
            print_info(f"  未选择有效项目（{account.name}）")
            continue
        
        # Confirm
        print_warn(f"  即将删除 {len(selected_projs)} 个项目及其自定义域名")
        if not confirm("输入 'yes' 确认"):
            print_info(f"  已取消 {account.name}")
            continue
        
        # Delete each project
        for item in selected_projs:
            proj_name = item["name"]
            print_header(f"  --- {proj_name} ---")
            
            # Delete custom domains
            for domain in item["domains"]:
                print_info(f"  正在删除域名 '{domain}' ...")
                result = api.delete_domain(proj_name, domain)
                if result and result.get("success"):
                    print_ok(f"    已删除域名 {domain}")
                else:
                    print_warn(f"    域名删除可能失败：{domain}")
            
            # Check deployments before deletion
            deps = api.list_deployments(proj_name)
            if len(deps) > 50:
                print_warn(f"    项目有 {len(deps)} 个部署")
                clean = input("    是否先删除旧部署？[y/N]: ").strip().lower()
                if clean == 'y':
                    sorted_deps = sorted(deps, key=lambda d: d.get("created_on", ""), reverse=True)
                    to_delete = sorted_deps[1:]  # skip newest
                    del_count = 0
                    for dep in to_delete:
                        result = api.delete_deployment(proj_name, dep.get("id", ""))
                        if result and result.get("success"):
                            del_count += 1
                        time.sleep(0.1)
                    print_ok(f"    已清理 {del_count} 个部署")
            
            # Delete project
            print_info(f"  正在删除项目 '{proj_name}' ...")
            result = api.delete_project(proj_name)
            if result and result.get("success"):
                print_ok(f"  已删除 {proj_name}")
            else:
                print_error(f"  失败：{proj_name}")
        
        # Optional KV namespace deletion
        kvs = api.list_kv_namespaces()
        if kvs:
            print_info(f"  找到 {len(kvs)} 个 KV 命名空间")
            if confirm("  是否删除 KV 命名空间？[y/N]"):
                # Show KV list
                kv_items = []
                for i, ns in enumerate(kvs, 1):
                    title = ns.get("title", "")
                    ns_id = ns.get("id", "")
                    bound = ""
                    for proj in projects:
                        configs = proj.get("deployment_configs", {})
                        for env_type in ["production", "preview"]:
                            kvs_in_proj = configs.get(env_type, {}).get("kv_namespaces", {})
                            if kvs_in_proj:
                                for binding_name, binding_info in kvs_in_proj.items():
                                    if binding_info.get("namespace_id") == ns_id:
                                        bound = "（已绑定项目）"
                    kv_items.append({"index": i, "title": title, "id": ns_id, "bound": bound})
                    print(f"  [{i}] {title}{bound}")
                
                print("  [A]ll 全部")
                print("  [Q]uit 退出")
                kv_sel = input("输入序号删除 KV 命名空间: ").strip()
                if kv_sel and kv_sel.lower() != 'q':
                    selected_kvs = parse_selection(kv_sel, kv_items)
                    if selected_kvs:
                        has_bound = any(kv.get("bound") for kv in selected_kvs)
                        if has_bound:
                            print_warn("  警告：选中的命名空间中部分仍绑定到项目")
                        if confirm("输入 'yes' 确认删除 KV"):
                            for kv in selected_kvs:
                                print_info(f"  正在删除 KV 命名空间 '{kv['title']}' ...")
                                result = api.delete_kv_namespace(kv["id"])
                                if result and result.get("success"):
                                    print_ok(f"    已删除 {kv['title']}")
                                else:
                                    print_error(f"    失败：{kv['title']}")
    
    print_ok("========== 删除完成 ==========")
    wait_enter()


def parse_selection(sel: str, items: list[dict]) -> list[dict]:
    """Parse user selection string (numbers, ranges, comma-separated)."""
    selected = []
    if sel.lower() == 'a':
        return items
    parts = [p.strip() for p in sel.split(',')]
    for part in parts:
        if '-' in part:
            try:
                start, end = part.split('-')
                start, end = int(start), int(end)
                selected.extend([item for item in items if start <= item["index"] <= end])
            except ValueError:
                continue
        else:
            try:
                n = int(part)
                selected.extend([item for item in items if item["index"] == n])
            except ValueError:
                continue
    seen = set()
    unique = []
    for item in selected:
        if item["index"] not in seen:
            seen.add(item["index"])
            unique.append(item)
    return unique
```

### ui.py 详细规格

```python
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from typing import Callable

console = Console()


def print_header(text: str):
    console.print(Panel.fit(text, border_style="cyan"))

def print_info(text: str):
    console.print(f"[cyan][INFO][/] {text}")

def print_ok(text: str):
    console.print(f"[green][OK][/]   {text}")

def print_warn(text: str):
    console.print(f"[yellow][WARN][/] {text}")

def print_error(text: str):
    console.print(f"[red][ERROR][/] {text}")

def wait_enter():
    console.print("\n[dim]按 Enter 返回菜单 ...[/]", end="")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def main_menu() -> int:
    """Show main menu. Returns 0=Quit, 1=Delete, 2=Deploy."""
    console.clear()
    menu = Panel.fit(
        "[bold cyan]Cloudflare Pages Manager[/]\n\n"
        "  [bold]1.[/]  批量删除    查询 CF → 删除自定义域 + 项目 + KV\n"
        "  [bold]2.[/]  批量部署    创建/更新 Pages 项目并上传源码\n"
        "  [bold]Q.[/]  退出",
        border_style="cyan",
    )
    console.print(menu)
    while True:
        choice = Prompt.ask("请选择", default="q")
        if choice.lower() == 'q':
            return 0
        elif choice == '1':
            return 1
        elif choice == '2':
            return 2
        else:
            print_warn("无效选择，请重新输入")


def select_accounts(accounts) -> list:
    """Interactive multi-account selection. Returns selected accounts list."""
    if not accounts:
        print_error("没有有效的账号")
        return []
    
    console.clear()
    table = Table(title="账号列表", border_style="yellow")
    table.add_column("#", style="bold")
    table.add_column("名称")
    table.add_column("项目")
    table.add_column("域名")
    
    for i, acct in enumerate(accounts, 1):
        domain = acct.pages.domain or ""
        table.add_row(str(i), acct.name, acct.pages.project_name, domain)
    
    console.print(table)
    console.print("\n[yellow][A]ll[/] 全部账号")
    console.print("[yellow][Q]uit[/] 退出\n")
    
    sel = Prompt.ask("请选择", default="q")
    if sel.lower() == 'q':
        return []
    if sel.lower() == 'a':
        return list(accounts)
    
    result = []
    for part in sel.split(','):
        part = part.strip()
        try:
            n = int(part) - 1
            if 0 <= n < len(accounts):
                result.append(accounts[n])
            else:
                print_warn(f"跳过无效序号：{part}")
        except ValueError:
            print_warn(f"跳过无效输入：{part}")
    
    if not result:
        print_error("未选择有效账号")
        return []
    return result


def confirm(prompt_text: str = "确认？") -> bool:
    """Ask for 'yes' confirmation."""
    response = Prompt.ask(prompt_text, default="no")
    return response.strip().lower() == 'yes'
```

### __main__.py 详细规格

```python
from .ui import main_menu, wait_enter, print_error
from .config import load_config
from .workflows import deploy_workflow, delete_workflow


def main():
    try:
        cfg = load_config()
    except FileNotFoundError as e:
        print_error(str(e))
        wait_enter()
        return
    
    while True:
        try:
            choice = main_menu()
            if choice == 0:
                break
            elif choice == 1:
                delete_workflow(cfg)
            elif choice == 2:
                deploy_workflow(cfg)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print_error(f"发生错误：{e}")
            wait_enter()


if __name__ == "__main__":
    main()
```

## 实现顺序

由于文件独立创建（无编译期依赖），所有 Task 可并行执行：

1. Task A: pyproject.toml + models.py + config.py + __init__.py
2. Task B: api.py
3. Task C: ui.py
4. Task D: workflows.py + __main__.py
