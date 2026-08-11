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
    """Converge managed environment variables and KV bindings."""
    manage_env = bool(account.pages.env)
    manage_kv = account.pages.kv_configured
    if not manage_env and not manage_kv:
        return True

    project = api.get_project(account.pages.project_name)
    if project is None:
        print_error("  查询 Pages 项目配置失败")
        return False

    target_envs = [account.pages.project_type] if account.pages.project_type in {"production", "preview"} else ["production", "preview"]
    current_configs = project.get("deployment_configs", {})
    dep_cfg: dict[str, dict] = {}
    managed_env_names = {ev.name for ev in account.pages.env if ev.name}
    desired_env_vars = {
        ev.name: {"value": ev.value, "type": ev.var_type}
        for ev in account.pages.env
        if ev.name and ev.value
    }

    for target_env in target_envs:
        current = current_configs.get(target_env, {})
        cfg: dict = {}
        if manage_env:
            current_env_vars = current.get("env_vars", {})
            cfg["env_vars"] = {
                **{name: None for name in current_env_vars if name not in managed_env_names},
                **desired_env_vars,
            }
        if manage_kv:
            current_kv = current.get("kv_namespaces", {})
            desired_kv = {}
            if account.pages.kv_binding:
                if not ns_id or not account.pages.kv_binding_env:
                    print_error("  KV 绑定已启用，但命名空间 ID 或绑定名无效")
                    return False
                desired_kv = {
                    account.pages.kv_binding_env: {"namespace_id": ns_id}
                }
            cfg["kv_namespaces"] = {
                **{name: None for name in current_kv if name not in desired_kv},
                **desired_kv,
            }
        dep_cfg[target_env] = cfg

    return api.patch_project_config(account.pages.project_name, dep_cfg)


def sync_project_domain(api: CfApiClient, project: str, target_domain: str) -> bool:
    """Replace all existing custom domains with the configured target domain."""
    domains = api.list_domains(project)
    if domains is None:
        print_error("  查询项目现有域名失败")
        return False

    existing_names: list[str] = []
    for domain in domains:
        name = domain.get("name")
        if isinstance(name, str) and name:
            existing_names.append(name)
    for domain in existing_names:
        if domain == target_domain:
            continue
        print_info(f"  正在删除旧域名 '{domain}' ...")
        result = api.delete_domain(project, domain)
        if not result or not result.get("success"):
            print_error(f"  删除旧域名失败：{domain}")
            return False
        print_ok(f"  旧域名已删除：{domain}")

    if target_domain in existing_names:
        print_ok(f"  域名已配置，跳过：{target_domain}")
        return True

    print_info(f"  正在添加域名 '{target_domain}' ...")
    result = api.add_domain(project, target_domain)
    if not result or not result.get("success"):
        print_error(f"  添加域名失败：{target_domain}")
        return False
    print_ok(f"  域名已添加：{target_domain}")
    return True


def sync_dns_record(api: CfApiClient, account: Account) -> bool:
    """Create or update the configured DNS record."""
    dns = account.dns
    if not dns.zone_id or not dns.name or not dns.content:
        return True

    print_info(f"  正在按内容查询 DNS 记录 '{dns.content}' ...")
    records = api.list_dns_records(dns.zone_id, dns.content)
    if records is None:
        error = api.last_error
        print_error(f"  查询 DNS 记录失败：{error}" if error else "  查询 DNS 记录失败")
        return False

    matches = [
        record
        for record in records
        if record.get("content") == dns.content
    ]

    if len(matches) > 1:
        keep = matches[0]
        for duplicate in matches[1:]:
            record_id = duplicate.get("id")
            if not isinstance(record_id, str) or not record_id:
                print_error(f"  重复 DNS 记录缺少 ID，无法删除：{dns.name}")
                return False
            result = api.delete_dns_record(dns.zone_id, record_id)
            if not result or not result.get("success"):
                print_error(f"  删除重复 DNS 记录失败：{dns.name}")
                return False
        matches = [keep]

    payload = {
        "type": dns.record_type,
        "name": dns.name,
        "content": dns.content,
        "proxied": dns.proxied,
        "ttl": dns.ttl,
    }
    if not matches:
        print_info(f"  正在创建 DNS 记录 '{dns.name}' ...")
        result = api.create_dns_record(dns.zone_id, payload)
        if not result or not result.get("success"):
            print_error(f"  创建 DNS 记录失败：{dns.name}")
            return False
        print_ok(f"  DNS 记录已创建：{dns.name} -> {dns.content}")
        return True

    current = matches[0]
    is_current = (
        str(current.get("name", "")).rstrip(".").lower() == dns.name.rstrip(".").lower()
        and str(current.get("type", "")).upper() == dns.record_type
        and current.get("proxied", False) is dns.proxied
        and current.get("ttl") == dns.ttl
    )
    if is_current:
        print_ok(f"  DNS 记录已是目标配置，跳过：{dns.name}")
        return True

    record_id = current.get("id")
    if not isinstance(record_id, str) or not record_id:
        print_error(f"  DNS 记录缺少 ID，无法修改：{dns.name}")
        return False
    print_info(f"  正在修改 DNS 记录 '{dns.name}' ...")
    result = api.update_dns_record(dns.zone_id, record_id, payload)
    if not result or not result.get("success"):
        print_error(f"  修改 DNS 记录失败：{dns.name}")
        return False
    print_ok(f"  DNS 记录已修改：{dns.name} -> {dns.content}")
    return True


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
        if api.get_project(project) is None:
            print_error("  创建或查询 Pages 项目失败")
            return False
        print_ok("  项目已存在，跳过创建")

    # ========== 第二步：上传部署 ==========
    print_info(f"  [2/4] 上传部署 ...")
    if not _run_wrangler(source_dir, project, account.token, account.account_id, "上传部署"):
        print_error("  上传部署失败")
        return False
    print_ok("  上传部署完成")

    # ========== 第三步：配置项目 ==========
    print_info(f"  [3/4] 配置项目 ...")

    # KV 命名空间：仅在启用绑定时创建或查询
    ns_id = None
    if account.pages.kv_create or account.pages.kv_binding:
        if not account.pages.kv_namespace:
            print_error("  KV 绑定已启用，但未配置 kv_namespace")
            return False
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
        if not ns_id:
            print_error(f"  无法获取 KV 命名空间 ID：{account.pages.kv_namespace}")
            return False

    # 设置环境变量 + KV 绑定
    if not set_project_config(api, account, ns_id):
        print_error("  环境变量或 KV 绑定配置失败")
        return False

    # 自定义域名
    if account.pages.domain and not sync_project_domain(api, project, account.pages.domain):
        return False

    # ========== 第四步：重新部署 ==========
    print_info(f"  [4/4] 重新部署使配置生效 ...")
    if not _run_wrangler(source_dir, project, account.token, account.account_id, "重新部署"):
        print_error("  重新部署失败")
        return False
    print_ok(f"  ✅ 项目 '{project}' 已完全部署并配置完成")

    # DNS 在项目部署后执行，但仍属于该账号的声明配置
    try:
        dns_synced = sync_dns_record(api, account)
    except Exception as exc:
        print_error(f"  DNS 同步异常：{exc}")
        dns_synced = False
    if not dns_synced:
        print_error(f"  DNS 同步失败，账号 '{account.name}' 配置未完全收敛")
        return False
    return True


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
    print_info("  5. 同步 DNS 记录（失败时当前账号标记失败）")
    print()

    print_info(">> 正在准备源码文件 ...")
    source_dir = prepare_source(cfg)
    if not source_dir:
        wait_enter()
        return

    source_dir = source_dir.resolve()

    results: list[tuple[Account, bool]] = []
    for account in selected:
        try:
            with CfApiClient(account.account_id, account.token) as api:
                success = deploy_project(api, account, source_dir)
        except Exception as exc:
            print_error(f"账号 '{account.name}' 执行异常：{exc}")
            success = False
        results.append((account, success))

    failed = [account for account, success in results if not success]
    print_ok("========== 部署完成 ==========")
    print_info(f"成功：{len(results) - len(failed)}，失败：{len(failed)}")
    for account in failed:
        print_error(f"  失败账号：{account.name}")
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
