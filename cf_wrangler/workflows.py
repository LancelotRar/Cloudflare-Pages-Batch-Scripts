import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import httpx

from .api import CfApiClient
from .config import get_enabled_accounts
from .models import Config, Account
from .ui import (
    confirm,
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


def set_project_config(api: CfApiClient, account: Account) -> bool:
    """Set environment variables and KV binding on a Pages project via PATCH."""
    env_vars = {}
    for ev in account.env:
        if ev.value:
            env_vars[ev.name] = {"value": ev.value, "type": ev.type}

    cfg = {}
    if env_vars:
        cfg["env_vars"] = env_vars

    if account.pages.kv_binding and account.pages.kv_namespace:
        ns_id = api.ensure_kv_namespace(account.pages.kv_namespace)
        if ns_id:
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


def _check_project_match(api: CfApiClient, project_data: dict, account: Account) -> tuple[bool, list[str]]:
    """比对项目实际配置与 YAML 是否一致，返回 (是否一致, 差异项列表)。"""
    diffs: list[str] = []

    # 1. KV 比对
    if account.pages.kv_binding and account.pages.kv_namespace:
        ns_id = None
        for ns in api.list_kv_namespaces():
            if ns.get("title") == account.pages.kv_namespace:
                ns_id = ns.get("id")
                break
        if not ns_id:
            diffs.append(f"KV 命名空间 '{account.pages.kv_namespace}' 不存在")
        else:
            bound = False
            for env_type in ["production", "preview"]:
                kvs = project_data.get("deployment_configs", {}).get(env_type, {}).get("kv_namespaces", {}) or {}
                for env_name, binding in kvs.items():
                    if env_name == account.pages.kv_binding_env and binding.get("namespace_id") == ns_id:
                        bound = True
            if not bound:
                diffs.append(f"KV 绑定 '{account.pages.kv_binding_env}' 未配置")
    elif account.pages.kv_binding:
        diffs.append("KV 绑定未配置（缺少 kv_namespace）")

    # 2. 域名比对
    if account.pages.domain:
        if account.pages.domain not in project_data.get("domains", []):
            diffs.append(f"域名 '{account.pages.domain}' 未添加")

    # 3. 环境变量比对（空值表示不设置，跳过比对）
    active_env = [ev for ev in account.env if ev.value]
    if active_env:
        dep_configs = project_data.get("deployment_configs", {})
        pt = account.pages.project_type
        current_vars: dict = {}
        # 从 production 或 preview 中收集当前 env_vars
        for et in [pt] if pt in ("production", "preview") else ["production", "preview"]:
            if et in dep_configs:
                current_vars.update(dep_configs[et].get("env_vars", {}) or {})
        for ev in active_env:
            actual = current_vars.get(ev.name)
            if not actual:
                diffs.append(f"环境变量 '{ev.name}' 未设置")
            elif actual.get("type") != ev.type:
                diffs.append(f"环境变量 '{ev.name}' 类型不匹配")
            elif ev.type != "secret_text" and actual.get("value") != ev.value:
                diffs.append(f"环境变量 '{ev.name}' 值不一致")

    return len(diffs) == 0, diffs


def deploy_project(api: CfApiClient, account: Account, source_dir: Path) -> bool:
    """Full deploy workflow for a single account."""
    project = account.pages.project_name
    print_header(f"部署：{account.name} → {project}")

    # ========== 第一步：检查项目 ==========
    print_info(f"  [1/4] 检查项目 '{project}' 是否存在 ...")
    existing = api.get_project(project)

    if existing:
        # 项目已存在 → 比对配置
        match, diffs = _check_project_match(api, existing, account)
        if match:
            print_ok("  项目配置与 YAML 一致，无需部署")
            return True
        print_ok("  项目已存在，但配置有差异，将重新部署")
        for d in diffs:
            print_info(f"    → {d}")
    else:
        print_info("  项目不存在，正在通过 API 创建 ...")
        result = api.create_project(project)
        if not (result and result.get("success")):
            print_error("  项目创建失败")
            return False
        print_ok("  项目已创建")

    # ========== 第二步：上传部署 ==========
    print_info(f"  [2/4] 上传部署：部署源码到 '{project}' ...")
    first_ok = _run_wrangler(source_dir, project, account.token, account.account_id, "上传部署")
    if not first_ok:
        print_error("  上传部署失败，请查看上方输出")
        return False
    print_ok("  上传部署完成")

    # ========== 第三步：配置项目 ==========
    print_info(f"  [3/4] 正在配置项目 ...")

    # 收集当前项目配置（用于判断哪些需要实际变更）
    current_vars: dict[str, dict] = {}
    # 新项目 KV 需要绑定，已有项目待检测
    kv_needs_binding = account.pages.kv_binding and not existing
    if existing:
        for et in ["production", "preview"]:
            dep = existing.get("deployment_configs", {}).get(et, {})
            current_vars.update(dep.get("env_vars", {}) or {})

    # KV 命名空间
    ns_id = None
    if account.pages.kv_namespace:
        all_ns = api.list_kv_namespaces()
        existing_ns = next((ns for ns in all_ns if ns.get("title") == account.pages.kv_namespace), None)
        if existing_ns:
            ns_id = existing_ns.get("id")
            print_ok(f"  KV 命名空间 '{account.pages.kv_namespace}' 已存在")
            # 检查该项目是否已绑定此命名空间
            if account.pages.kv_binding and ns_id and existing:
                found = False
                for et in ["production", "preview"]:
                    kvs = existing.get("deployment_configs", {}).get(et, {}).get("kv_namespaces", {}) or {}
                    for _, binding in kvs.items():
                        if binding.get("namespace_id") == ns_id:
                            found = True
                            break
                    if found:
                        break
                kv_needs_binding = not found
        elif account.pages.kv_create:
            result = api.create_kv_namespace(account.pages.kv_namespace)
            if result and result.get("success"):
                ns_id = result["result"].get("id")
                kv_needs_binding = True
                print_ok(f"  KV 命名空间 '{account.pages.kv_namespace}' 已创建")

    # 过滤掉值为空的 env（空值 = 不设置该项）
    active_env = [ev for ev in account.env if ev.value]

    # 判断哪些环境变量需要设置
    env_to_set: list[str] = []
    for ev in active_env:
        actual = current_vars.get(ev.name)
        if not actual:
            env_to_set.append(ev.name)
        elif actual.get("type") != ev.type:
            env_to_set.append(ev.name)
        elif ev.type != "secret_text" and actual.get("value") != ev.value:
            env_to_set.append(ev.name)

    # 有变更时才 PATCH
    need_patch = bool(env_to_set) or kv_needs_binding
    if need_patch:
        patch_ok = set_project_config(api, account)
        if not patch_ok:
            print_warn("  项目配置（环境变量/KV绑定）可能未完全生效")
        else:
            for ev in active_env:
                if ev.name in env_to_set:
                    val_desc = "已设置（secret）" if ev.type == "secret_text" else "已设置"
                    print_ok(f"  变量 {ev.name} {val_desc}")
            if kv_needs_binding:
                print_ok("  KV 变量已设置")
                print_ok("  KV 已绑定")
    else:
        if active_env:
            for ev in active_env:
                print_ok(f"  变量 {ev.name} 一致，跳过")
        if account.pages.kv_binding and account.pages.kv_namespace and not kv_needs_binding:
            print_ok("  KV 已绑定，一致")

    # 自定义域名
    if account.pages.domain:
        if existing and account.pages.domain in existing.get("domains", []):
            print_ok(f"  域名 '{account.pages.domain}' 已配置，跳过")
        else:
            print_info(f"  正在添加域名 '{account.pages.domain}' ...")
            result = api.add_domain(project, account.pages.domain)
            if result and result.get("success"):
                print_ok(f"  域名 '{account.pages.domain}' 已添加")
            else:
                print_warn("  域名添加可能失败或已存在")

    # ========== 第四步：重新部署（配置生效）==========
    print_info(f"  [4/4] 重新部署使配置生效 ...")
    second_ok = _run_wrangler(source_dir, project, account.token, account.account_id, "重新部署")
    if second_ok:
        print_ok(f"  ✅ 项目 '{project}' 已完全部署并配置完成")
        return True
    else:
        print_error("  重新部署可能失败，请查看上方输出")
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
    print_info("  1. 确保项目存在（通过 CF API 创建）")
    print_info("  2. 第 1 次部署：wrangler pages deploy（部署源码）")
    print_info("  3. 配置项目：创建 KV 命名空间 → 设置环境变量 + KV 绑定 → 添加自定义域名")
    print_info("  4. 第 2 次部署：wrangler pages deploy（配置生效后重新部署）")
    print()

    print_info(">> 正在准备源码文件 ...")
    source_dir = prepare_source(cfg)
    if not source_dir:
        wait_enter()
        return

    source_dir = source_dir.resolve()

    for account in selected:
        api = CfApiClient(account.account_id, account.token)
        try:
            deploy_project(api, account, source_dir)
        finally:
            api.close()

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
                selected.extend(
                    [item for item in items if start <= item["index"] <= end]
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
    print_info("将对每个账号依次执行：")
    print_info("  1. 从 Cloudflare 列出项目")
    print_info("  2. 选择要删除的项目（同时删除自定义域名 + 项目）")
    print_info("  3. 可选：删除 KV 命名空间")
    print()

    for account in selected_accounts:
        api = CfApiClient(account.account_id, account.token)
        try:
            print_header(f"--- {account.name} ---")

            print_info("正在查询项目 ...")
            projects = api.list_projects()

            if projects:
                proj_items = []
                for i, proj in enumerate(projects, 1):
                    name = proj.get("name", "")
                    domains = [
                        d for d in proj.get("domains", [])
                        if d != f"{name}.pages.dev"
                    ]
                    domain_str = f" | 域名：{', '.join(domains)}" if domains else ""
                    proj_items.append({
                        "index": i,
                        "name": name,
                        "project": proj,
                        "domains": domains,
                    })
                    print(f"  [{i}] {name}{domain_str}")

                print("  [A]ll 全部")
                print("  [Q]uit 退出")
                print()

                sel = input("输入序号删除（如 '1,3' 或 '1-3'），[A]ll 全选，回车跳过: ").strip()
                if sel and sel.lower() != "q":
                    selected_projs = parse_selection(sel, proj_items)
                    if selected_projs:
                        print_warn(f"  即将删除 {len(selected_projs)} 个项目及其自定义域名")
                        if confirm("输入 'yes' 确认"):
                            for item in selected_projs:
                                proj_name = item["name"]
                                print(f"\n  --- {proj_name} ---")

                                for domain in item["domains"]:
                                    print_info(f"  正在删除域名 '{domain}' ...")
                                    result = api.delete_domain(proj_name, domain)
                                    if result and result.get("success"):
                                        print_ok(f"    已删除域名 {domain}")
                                    else:
                                        print_warn(f"    域名删除可能失败：{domain}")

                                deps = api.list_deployments(proj_name)
                                if len(deps) > 50:
                                    print_warn(f"    项目有 {len(deps)} 个部署")
                                    clean = input("    是否先删除旧部署？（超过 50 个需先清理）[y/N]: ").strip().lower()
                                    if clean == "y":
                                        sorted_deps = sorted(deps, key=lambda d: d.get("created_on", ""), reverse=True)
                                        to_delete = sorted_deps[1:]
                                        del_count = 0
                                        for dep in to_delete:
                                            dep_id = dep.get("id", "")
                                            result = api.delete_deployment(proj_name, dep_id)
                                            if result and result.get("success"):
                                                del_count += 1
                                            time.sleep(0.1)
                                        print_ok(f"    已清理 {del_count} 个部署")

                                print_info(f"  正在删除项目 '{proj_name}' ...")
                                result = api.delete_project(proj_name)
                                if result and result.get("success"):
                                    print_ok(f"  已删除 {proj_name}")
                                else:
                                    print_error(f"  失败：{proj_name}")
            else:
                print_info(f"  {account.name} 未找到 Pages 项目，跳过项目删除")

            # 无论是否有 Pages 项目，都继续处理 KV 命名空间
            # 刷新项目列表，确保绑定检测不引用已删除的项目
            projects = api.list_projects()
            print(f"\n  --- {account.name} 的 KV 命名空间 ---")
            kvs = api.list_kv_namespaces()
            if kvs:
                print_info(f"  找到 {len(kvs)} 个 KV 命名空间")
                kv_items = []
                for i, ns in enumerate(kvs, 1):
                    title = ns.get("title", "")
                    ns_id = ns.get("id", "")
                    bound = ""
                    for proj in projects or []:
                        configs = proj.get("deployment_configs", {})
                        for env_type in ["production", "preview"]:
                            kvs_in_proj = configs.get(env_type, {}).get("kv_namespaces", {}) or {}
                            for _, binding_info in kvs_in_proj.items():
                                if binding_info.get("namespace_id") == ns_id:
                                    bound = "（已绑定项目）"
                    kv_items.append({"index": i, "title": title, "id": ns_id, "bound": bound})
                    print(f"  [{i}] {title}{bound}")

                print("  [A]ll 全部")
                print("  [Q]uit 退出")
                kv_sel = input("输入序号删除 KV 命名空间: ").strip()
                if kv_sel and kv_sel.lower() != "q":
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
            else:
                print_info("  未找到 KV 命名空间")
        finally:
            api.close()

    print_ok("========== 删除完成 ==========")
    wait_enter()
