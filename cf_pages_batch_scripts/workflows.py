import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx

from .api import CfApiClient
from .config import get_enabled_accounts
from .models import Config, Account
from .ui import (
    print_error,
    print_header,
    print_info,
    print_ok,
    print_warn,
    select_accounts,
    wait_enter,
)


def prepare_source(cfg: Config) -> Path | None:
    """Download and extract source code from files_to_redeploy URL."""
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
    deploy_dir.mkdir(parents=True, exist_ok=True)

    try:
        resp = httpx.get(fr.download_url, timeout=300, follow_redirects=True)
        resp.raise_for_status()

        zip_path = deploy_dir / "source.zip"
        zip_path.write_bytes(resp.content)

        # 基本完整性检查：ZIP 至少应有几百字节
        if zip_path.stat().st_size < 200:
            raise ValueError(f"下载文件过小 ({zip_path.stat().st_size} bytes)，可能不是有效的 ZIP")

        extracted = deploy_dir / "extracted"
        extracted.mkdir(exist_ok=True)

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)

        # Find source directory: use single top-level dir if exists, otherwise root
        dirs = [d for d in extracted.iterdir() if d.is_dir()]
        if len(dirs) == 1:
            src = dirs[0]
        else:
            src = extracted

        print_ok(f"源码已就绪：{src}")
        return src
    except Exception as e:
        print_error(f"下载/解压失败：{e}")
        return None


def set_project_config(api: CfApiClient, account: Account, ns_id: str | None = None) -> bool:
    """Set environment variables and KV binding on a Pages project via PATCH."""
    env_vars = {}
    for ev in account.env:
        if ev.value:
            env_vars[ev.name] = {"value": ev.value, "type": ev.var_type}

    cfg = {}
    if env_vars:
        cfg["env_vars"] = env_vars

    if account.pages.kv_binding and ns_id:
        cfg["kv_namespaces"] = {
            account.pages.kv_binding_env: {"namespace_id": ns_id}
        }

    if not cfg:
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


def _find_wrangler() -> str | None:
    """Find wrangler CLI path via PATH resolution, with platform fallbacks."""
    wrangler_path = shutil.which("wrangler")
    if wrangler_path:
        return wrangler_path
    # Fallback: common npm global install paths
    candidates = []
    if os.name == "nt":
        candidates = [
            os.path.expanduser(r"~\AppData\Roaming\npm\wrangler.cmd"),
            r"C:\Program Files\nodejs\wrangler.cmd",
        ]
    else:
        candidates = [
            "/usr/local/bin/wrangler",
            os.path.expanduser("~/.npm-global/bin/wrangler"),
        ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _run_wrangler(source_dir: Path, project: str, token: str, account_id: str, step_label: str) -> bool:
    """Run `wrangler pages deploy` and return success status."""
    wrangler_exe = _find_wrangler()
    if not wrangler_exe:
        print_error("  未找到 wrangler CLI，请安装：npm install -g wrangler")
        return False

    env = {
        **os.environ,
        "CLOUDFLARE_API_TOKEN": token,
        "CLOUDFLARE_ACCOUNT_ID": account_id,
    }
    try:
        result = subprocess.run(
            [
                wrangler_exe,
                "pages",
                "deploy",
                str(source_dir),
                "--project-name",
                project,
                "--branch",
                "main",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )
        output = (result.stdout or "") + (result.stderr or "")
        for line in output.splitlines():
            print(f"    {line}")

        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print_error(f"  {step_label}超时")
        return False
    except FileNotFoundError:
        print_error("  未找到 wrangler CLI，请先安装：npm install -g wrangler")
        return False
    except Exception as e:
        print_error(f"  {step_label}异常：{e}")
        return False


def deploy_project(api: CfApiClient, account: Account, source_dir: Path) -> bool:
    """Full deploy workflow for a single account."""
    project = account.pages.project_name
    print_header(f"部署：{account.name} → {project}")

    # ========== 第一步：创建项目 ==========
    print_info(f"  [1/4] 创建项目 '{project}' ...")
    result = api.create_project(project)
    if result and result.get("success"):
        print_ok("  项目已创建")
    else:
        print_warn("  项目可能已存在，继续部署")

    # ========== 第二步：上传部署 ==========
    print_info(f"  [2/4] 上传部署 ...")
    if not _run_wrangler(source_dir, project, account.token, account.account_id, "上传部署"):
        print_error("  上传部署失败")
        return False
    print_ok("  上传部署完成")

    # ========== 第三步：配置项目 ==========
    print_info(f"  [3/4] 配置项目 ...")

    # KV 命名空间：先创建，失败则查询获取 ID
    ns_id = None
    if account.pages.kv_namespace:
        result = api.create_kv_namespace(account.pages.kv_namespace)
        if result and result.get("success"):
            ns_id = result["result"].get("id")
            print_ok(f"  KV 命名空间 '{account.pages.kv_namespace}' 已创建")
        else:
            for ns in api.list_kv_namespaces():
                if ns.get("title") == account.pages.kv_namespace:
                    ns_id = ns.get("id")
                    print_ok(f"  KV 命名空间 '{account.pages.kv_namespace}' 已存在")
                    break

    # 设置环境变量 + KV 绑定
    set_project_config(api, account, ns_id)

    # 自定义域名
    if account.pages.domain:
        print_info(f"  正在添加域名 '{account.pages.domain}' ...")
        result = api.add_domain(project, account.pages.domain)
        if result and result.get("success"):
            print_ok(f"  域名已添加：{account.pages.domain}")
        else:
            print_warn(f"  域名可能已存在：{account.pages.domain}")

    # ========== 第四步：重新部署 ==========
    print_info(f"  [4/4] 重新部署使配置生效 ...")
    if _run_wrangler(source_dir, project, account.token, account.account_id, "重新部署"):
        print_ok(f"  ✅ 项目 '{project}' 已完全部署并配置完成")
        return True
    else:
        print_error("  重新部署失败")
        return False


def deploy_workflow(cfg: Config):
    """完整部署流程入口"""
    accounts = get_enabled_accounts(cfg)
    if not accounts:
        print_error("没有已启用的账号（请检查 config.yaml 中的 enabled 字段）")
        wait_enter()
        return

    # 提前检查 wrangler 是否可用，避免下载源码后才发现
    if not _find_wrangler():
        print_error("未找到 wrangler CLI，请先安装：npm install -g wrangler")
        wait_enter()
        return

    selected = select_accounts(accounts)
    if not selected:
        return

    print_header("部署项目")
    print_info("将对每个账号依次执行：")
    print_info("  1. 创建项目（通过 CF API）")
    print_info("  2. 部署源码：wrangler pages deploy")
    print_info("  3. 配置项目：KV 命名空间 → 环境变量 + KV 绑定 → 自定义域名")
    print_info("  4. 重新部署使配置生效")
    print()

    print_info(">> 正在准备源码文件 ...")
    source_dir = prepare_source(cfg)
    if not source_dir:
        wait_enter()
        return

    source_dir = source_dir.resolve()

    for account in selected:
        with CfApiClient(account.account_id, account.token) as api:
            deploy_project(api, account, source_dir)

    print_ok("========== 部署完成 ==========")
    wait_enter()


def parse_selection(sel: str, items: list[dict]) -> list[dict]:
    """解析用户选择字符串，返回去重后的条目列表。"""
    selected = []
    sel_lower = sel.strip().lower()

    if sel_lower == "a":
        return list(items)

    parts = [p.strip() for p in sel.split(",")]
    for part in parts:
        if not part:
            continue
        if "-" in part:
            try:
                start_str, end_str = part.split("-", 1)
                start, end = int(start_str.strip()), int(end_str.strip())
                lo, hi = (start, end) if start <= end else (end, start)
                selected.extend(
                    [item for item in items if lo <= item["index"] <= hi]
                )
            except (ValueError, IndexError):
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


def delete_workflow(cfg: Config):
    """完整删除流程入口"""
    accounts = get_enabled_accounts(cfg)
    if not accounts:
        print_error("没有已启用的账号（请检查 config.yaml 中的 enabled 字段）")
        wait_enter()
        return

    selected_accounts = select_accounts(accounts)
    if not selected_accounts:
        return

    print_header("批量删除")

    for account in selected_accounts:
        with CfApiClient(account.account_id, account.token) as api:
            print_header(f"--- {account.name} ---")

            # 按配置删除自定义域名
            domain = account.pages.domain
            proj_name = account.pages.project_name
            if domain:
                print_info(f"正在删除域名 '{domain}' ...")
                result = api.delete_domain(proj_name, domain)
                if result and result.get("success"):
                    print_ok(f"    已删除域名 {domain}")
                else:
                    print_warn(f"    域名删除可能失败：{domain}")

            # 按配置删除项目
            print_info(f"正在删除项目 '{proj_name}' ...")
            result = api.delete_project(proj_name)
            if result and result.get("success"):
                print_ok(f"  已删除 {proj_name}")
            else:
                print_warn(f"  项目 '{proj_name}' 不存在或删除失败")

            # 按配置删除 KV 命名空间（需查 ID，唯一一次 CF 查询）
            kv_name = account.pages.kv_namespace
            if kv_name:
                for ns in api.list_kv_namespaces():
                    if ns.get("title") == kv_name:
                        ns_id = ns.get("id", "")
                        print_info(f"正在删除 KV 命名空间 '{kv_name}' ...")
                        result = api.delete_kv_namespace(ns_id)
                        if result and result.get("success"):
                            print_ok(f"  已删除 {kv_name}")
                        else:
                            print_error(f"  失败：{kv_name}")
                        break
                else:
                    print_info(f"  KV 命名空间 '{kv_name}' 不存在，跳过")

    print_ok("========== 删除完成 ==========")
    wait_enter()
