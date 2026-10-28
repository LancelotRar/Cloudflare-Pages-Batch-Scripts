import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import httpx
from rich.markup import escape

from .api import CfApiClient, shared_ssl_context
from .config import get_enabled_accounts
from .models import Account, Config, EnvVar
from .ui import (
    print_error,
    print_header,
    print_info,
    print_ok,
    print_warn,
    select_accounts,
    wait_enter,
)

# 直连或代理线路会偶发 TLS 握手中断（OpenSSL UNEXPECTED_EOF_WHILE_READING），
# 单次失败不应让整轮批量部署中止，故下载阶段做有限次重试。
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_RETRY_BACKOFF = (2, 4, 8)
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=30.0, pool=20.0)


def _download(url: str, proxy: str) -> bytes:
    """下载 URL 内容；仅对网络层瞬时错误重试，HTTP 状态错误直接抛出。"""
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = httpx.get(
                url,
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                proxy=proxy or None,
                verify=shared_ssl_context(),
            )
            resp.raise_for_status()
            return resp.content
        except httpx.TransportError as exc:
            if attempt >= DOWNLOAD_ATTEMPTS:
                raise
            wait = DOWNLOAD_RETRY_BACKOFF[attempt - 1]
            print_warn(f"下载失败（第 {attempt}/{DOWNLOAD_ATTEMPTS} 次）：{exc}，{wait}s 后重试 ...")
            time.sleep(wait)


def prepare_source(cfg: Config) -> Path | None:
    """Download and extract source code from files_to_redeploy URL.

    下载与解压先进入同级临时目录，全部成功后才原子交换到目标目录；
    下载失败不会破坏上一次的可用源码。
    """
    fr = cfg.files_to_redeploy
    deploy_dir = Path(fr.dir)
    if not deploy_dir.is_absolute():
        deploy_dir = Path(__file__).resolve().parent.parent / fr.dir
    deploy_dir = deploy_dir.resolve()

    if not fr.download_url:
        print_error("未配置 files_to_redeploy.download_url")
        return None

    print_info(f"正在从 {fr.download_url} 下载最新源码 ...")
    tmp_dir = deploy_dir.parent / f".{deploy_dir.name}.download.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        zip_path = tmp_dir / "source.zip"
        zip_path.write_bytes(_download(fr.download_url, cfg.proxy))

        # 基本完整性检查：ZIP 至少应有几百字节
        if zip_path.stat().st_size < 200:
            raise ValueError(f"下载文件过小 ({zip_path.stat().st_size} bytes)，可能不是有效的 ZIP")

        extracted = tmp_dir / "extracted"
        extracted.mkdir(exist_ok=True)

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)

        # Find source directory: use single top-level dir if exists, otherwise root
        dirs = [d for d in extracted.iterdir() if d.is_dir()]
        src = dirs[0] if len(dirs) == 1 else extracted
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print_error(f"下载/解压失败：{e}")
        return None

    # 原子交换：一切成功后才替换旧目录
    if deploy_dir.exists():
        shutil.rmtree(deploy_dir)
    shutil.move(str(tmp_dir), str(deploy_dir))

    src = deploy_dir / src.relative_to(tmp_dir)
    print_ok(f"源码已就绪：{src}")
    return src


def _format_env_var(ev: EnvVar) -> str:
    """环境变量单行展示：名称 = 值 [类型]。值可能含方括号，需转义以免被当作 Rich 标记。"""
    return escape(f"{ev.name} = {ev.value} [{ev.var_type}]")


def set_project_config(api: CfApiClient, account: Account, ns_id: str | None = None) -> bool:
    """Converge managed environment variables and KV bindings."""
    manage_env = bool(account.pages.env)
    manage_kv = account.pages.kv_create
    if not manage_env and not manage_kv:
        return True

    project = api.get_project(account.pages.project_name)
    if project is None:
        print_error("  查询 Pages 项目配置失败")
        return False

    env_key = account.pages.project_type
    target_envs = [env_key] if env_key in {"production", "preview"} else ["production", "preview"]
    current_configs = project.get("deployment_configs", {}) or {}
    dep_cfg: dict[str, dict] = {}
    managed_env_names = {ev.name for ev in account.pages.env if ev.name}
    desired_env_vars = {
        ev.name: {"value": ev.value, "type": ev.var_type} for ev in account.pages.env if ev.name and ev.value
    }

    if manage_env:
        if desired_env_vars:
            print_info(
                f"    环境变量（受管理白名单，共 {len(desired_env_vars)} 个）→ 目标环境：{'、'.join(target_envs)}"
            )
            for ev in account.pages.env:
                if ev.name and ev.value:
                    print_info(f"      - {_format_env_var(ev)}")
        else:
            print_info("    没有可写入的环境变量，跳过")
        empty_names = [ev.name for ev in account.pages.env if ev.name and not ev.value]
        if empty_names:
            print_warn(f"    变量 {'、'.join(empty_names)} 的值为空，不会写入（同名已有变量会被移除）")

    if manage_kv and (not ns_id or not account.pages.kv_binding_env):
        print_error("  KV 已启用，但命名空间 ID 或绑定名无效")
        return False

    removed_env_names: list[str] = []
    for target_env in target_envs:
        current = current_configs.get(target_env, {}) or {}
        cfg: dict = {}
        if manage_env:
            current_env_vars = current.get("env_vars", {}) or {}
            for name in current_env_vars:
                if name not in managed_env_names and name not in removed_env_names:
                    removed_env_names.append(name)
            cfg["env_vars"] = {
                **{name: None for name in current_env_vars if name not in managed_env_names},
                **desired_env_vars,
            }
        if manage_kv:
            current_kv = current.get("kv_namespaces", {}) or {}
            desired_kv = {account.pages.kv_binding_env: {"namespace_id": ns_id}}
            cfg["kv_namespaces"] = {
                **{name: None for name in current_kv if name not in desired_kv},
                **desired_kv,
            }
        dep_cfg[target_env] = cfg

    if removed_env_names:
        print_warn(f"    将删除项目上未声明的变量：{'、'.join(removed_env_names)}")
    if manage_kv:
        print_info(f"    KV 绑定：{account.pages.kv_binding_env} -> {ns_id}")

    ok = api.patch_project_config(account.pages.project_name, dep_cfg)
    if ok:
        summary: list[str] = []
        if manage_env:
            summary.append(f"环境变量 {len(desired_env_vars)} 个")
        if manage_kv:
            summary.append("KV 绑定 1 个")
        print_ok(f"    项目配置已提交：{' / '.join(summary)}")
    return ok


def sync_project_domain(api: CfApiClient, project: str, target_domain: str) -> bool:
    """Replace all existing custom domains with the configured target domain."""
    domains = api.list_domains(project)
    if domains is None:
        print_error("  查询项目现有域名失败")
        return False

    existing_names: list[str] = []
    for entry in domains:
        name = entry.get("name")
        if isinstance(name, str) and name:
            existing_names.append(name)
    for existing in existing_names:
        if existing == target_domain:
            continue
        print_info(f"  正在删除旧域名 '{existing}' ...")
        result = api.delete_domain(project, existing)
        if not result or not result.get("success"):
            print_error(f"  删除旧域名失败：{existing}")
            return False
        print_ok(f"  旧域名已删除：{existing}")

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
    """Create or update the configured DNS record.

    记录名取 pages.domain，目标值默认由 <project_name>.pages.dev 拼接。
    """
    dns = account.dns
    domain = account.pages.domain
    content = account.dns_content
    if not dns.zone_id or not domain or not content:
        return True

    print_info(f"  正在按内容查询 DNS 记录 '{content}' ...")
    records = api.list_dns_records(dns.zone_id, content)
    if records is None:
        error = api.last_error
        print_error(f"  查询 DNS 记录失败：{error}" if error else "  查询 DNS 记录失败")
        return False

    matches = [record for record in records if record.get("content") == content]

    if len(matches) > 1:
        keep = matches[0]
        for duplicate in matches[1:]:
            record_id = duplicate.get("id")
            if not isinstance(record_id, str) or not record_id:
                print_error(f"  重复 DNS 记录缺少 ID，无法删除：{domain}")
                return False
            result = api.delete_dns_record(dns.zone_id, record_id)
            if not result or not result.get("success"):
                print_error(f"  删除重复 DNS 记录失败：{domain}")
                return False
        matches = [keep]

    payload = {
        "type": dns.record_type,
        "name": domain,
        "content": content,
        "proxied": dns.proxied,
        "ttl": dns.ttl,
    }
    if not matches:
        print_info(f"  正在创建 DNS 记录 '{domain}' ...")
        result = api.create_dns_record(dns.zone_id, payload)
        if not result or not result.get("success"):
            print_error(f"  创建 DNS 记录失败：{domain}")
            return False
        print_ok(f"  DNS 记录已创建：{domain} -> {content}")
        return True

    current = matches[0]
    is_current = (
        str(current.get("name", "")).rstrip(".").lower() == domain.rstrip(".").lower()
        and str(current.get("type", "")).upper() == dns.record_type
        and current.get("proxied", False) is dns.proxied
        and current.get("ttl") == dns.ttl
    )
    if is_current:
        print_ok(f"  DNS 记录已是目标配置，跳过：{domain}")
        return True

    record_id = current.get("id")
    if not isinstance(record_id, str) or not record_id:
        print_error(f"  DNS 记录缺少 ID，无法修改：{domain}")
        return False
    print_info(f"  正在修改 DNS 记录 '{domain}' ...")
    result = api.update_dns_record(dns.zone_id, record_id, payload)
    if not result or not result.get("success"):
        print_error(f"  修改 DNS 记录失败：{domain}")
        return False
    print_ok(f"  DNS 记录已修改：{domain} -> {content}")
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


def _wrangler_error_hint(output: str) -> str | None:
    """把 wrangler 常见英文报错映射为中文处置提示；未识别的错误返回 None。"""
    text = output.lower()

    if "8000119" in text or "has been blocked" in text:
        return (
            "项目已被 Cloudflare 封禁（8000119），重试不会生效；"
            "请邮件 abusereply@cloudflare.com 申诉，附账号 ID、项目名和 wrangler 日志路径"
        )
    if (
        "authentication error" in text
        or "invalid access token" in text
        or "correct permissions for this operation" in text
    ):
        return (
            "Cloudflare 认证失败（10000/9109）：token 可能无效、过期或与 account_id 不匹配；"
            "请确认 token 归属的账号与 account_id 一致"
        )
    if "insufficient permissions" in text or "do not have permission" in text:
        return "Cloudflare 权限不足：token 需具备 Pages 项目的编辑权限（Account → Cloudflare Pages → Edit）"
    if any(needle in text for needle in ("fetch failed", "econnreset", "etimedout", "enotfound", "getaddrinfo")):
        return "wrangler 访问 Cloudflare API 网络失败：请检查 HTTPS_PROXY / HTTP_PROXY 代理和网络连通性"

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
                # 仓库有未提交改动时不再打印 dirty 警告；也避免 wrangler
                # 把新静态项目自动委派成 Worker
                "--commit-dirty=true",
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

        if result.returncode != 0:
            hint = _wrangler_error_hint(output)
            if hint:
                print_error(f"  {hint}")

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


def deploy_project(api: CfApiClient, account: Account, source_dir: Path, proxy: str = "") -> bool:
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

    # ========== 第二步：配置项目 ==========
    print_info("  [2/4] 配置项目（环境变量 / KV 绑定 / 自定义域名）...")

    # KV 总开关：kv_create=false 时不进行任何 KV 操作；
    # true 时要求绑定配置完整，命名空间查询优先、缺失时创建（幂等）
    ns_id: str | None = None
    if account.pages.kv_create:
        if not account.pages.kv_namespace:
            print_error("  KV 已启用，但未配置 kv_namespace")
            return False
        if not account.pages.kv_binding or not account.pages.kv_binding_env:
            print_error("  KV 已启用，但未配置 kv_binding / kv_binding_env")
            return False
        ns_id, ns_created = api.ensure_kv_namespace(account.pages.kv_namespace)
        if not ns_id:
            print_error(f"  无法获取 KV 命名空间 ID：{account.pages.kv_namespace}（{api.last_error or '未知原因'}）")
            return False
        print_ok(f"  KV 命名空间 '{account.pages.kv_namespace}' {'已创建' if ns_created else '已存在'}")

    # 设置环境变量 + KV 绑定
    if not set_project_config(api, account, ns_id):
        print_error(f"  环境变量或 KV 绑定配置失败：{api.last_error or '未知原因'}")
        return False

    # 自定义域名
    if account.pages.domain and not sync_project_domain(api, project, account.pages.domain):
        return False

    # ========== 第三步：同步 DNS ==========
    print_info("  [3/4] 同步 DNS 记录 ...")
    try:
        dns_configured = bool(account.dns.zone_id and account.pages.domain and account.dns_content)
        if not dns_configured:
            dns_synced = True
        elif not account.dns.token:
            print_error("  DNS 配置缺少独立的 dns_token")
            dns_synced = False
        else:
            with CfApiClient(account.account_id, account.dns.token, proxy=proxy) as dns_api:
                dns_synced = sync_dns_record(dns_api, account)
    except Exception as exc:
        print_error(f"  DNS 同步异常：{exc}")
        dns_synced = False
    if not dns_synced:
        print_error(f"  DNS 同步失败，账号 '{account.name}' 配置未完全收敛")
        return False

    # ========== 第四步：部署（仅一次） ==========
    # Pages 的项目配置在下一次部署时注入，因此环境变量 / KV 绑定 / 域名必须全部
    # 在部署之前收敛，一次部署即可生效，不再依赖“配置后重新部署”。
    print_info("  [4/4] 上传部署（本次部署即携带上述配置）...")
    if not _run_wrangler(source_dir, project, account.token, account.account_id, "上传部署"):
        print_error("  上传部署失败")
        return False
    carried = [f"域名 {account.pages.domain or '未配置'}"]
    env_count = sum(1 for ev in account.pages.env if ev.name and ev.value)
    if env_count:
        carried.append(f"环境变量 {env_count} 个")
    if account.pages.kv_create and account.pages.kv_binding_env:
        carried.append(f"KV 绑定 {account.pages.kv_binding_env}")
    print_ok(f"  ✅ 项目 '{project}' 已一次部署完成，配置随本次部署生效：{' / '.join(carried)}")
    return True


def deploy_workflow(cfg: Config) -> None:
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
    print_info("  2. 配置项目：KV 命名空间 → 环境变量 + KV 绑定 → 自定义域名")
    print_info("  3. 同步 DNS 记录（失败时当前账号标记失败）")
    print_info("  4. 部署源码：wrangler pages deploy（单次部署即携带上述配置）")
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
            with CfApiClient(account.account_id, account.token, proxy=cfg.proxy) as api:
                success = deploy_project(api, account, source_dir, proxy=cfg.proxy)
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


def _collect_account_resources(api: CfApiClient, account: Account, proxy: str = "") -> dict | None:
    """实时查询账号下全部可删除资源；任一查询失败返回 None。

    返回 {"projects": [{"name", "domains"}], "kv": [...], "dns": [...], "dns_skipped": int}。
    DNS 只收录「记录名 = 本账号项目已绑定域名」且「记录值 = 该项目 .pages.dev」的 CNAME，
    其余记录计入 dns_skipped 并保留，避免误删同 Zone 内其他账号或业务的数据。
    """
    projects = api.list_projects()
    if projects is None:
        print_error(f"  查询账号下全部 Pages 项目失败：{api.last_error or '未知原因'}")
        return None

    entries: list[dict] = []
    for project in projects:
        name = project.get("name")
        if not isinstance(name, str) or not name:
            continue
        domains = api.list_domains(name)
        if domains is None:
            print_error(f"  查询项目 '{name}' 的全部自定义域失败：{api.last_error or '未知原因'}")
            return None
        entries.append(
            {
                "name": name,
                "domains": [d["name"] for d in domains if isinstance(d.get("name"), str) and d["name"]],
            }
        )

    namespaces = api.list_kv_namespaces()
    if namespaces is None:
        print_error(f"  查询账号下全部 KV 命名空间失败：{api.last_error or '未知原因'}")
        return None
    kv = [ns for ns in namespaces if isinstance(ns.get("id"), str) and ns["id"]]

    dns_records: list[dict] = []
    dns_skipped = 0
    if account.dns.zone_id:
        if not account.dns.token:
            print_error("  已配置 dns.zone_id 但缺少 dns_token，无法查询 DNS 记录")
            return None
        # DNS 使用托管域名账户的独立 token，不能用 Pages 账户的 token
        with CfApiClient(account.account_id, account.dns.token, proxy=proxy) as dns_api:
            records = dns_api.list_zone_dns_records(account.dns.zone_id)
        if records is None:
            print_error(f"  查询 Zone 内全部 DNS 记录失败：{dns_api.last_error or '未知原因'}")
            return None
        # 只清理「记录名 = 本账号项目已绑定的自定义域」且「记录值 = 该项目 .pages.dev」
        # 的 CNAME。Zone 可能被多个账号共享，只按 content 匹配会误删其他账号的记录。
        targets = {
            (domain.lower().rstrip("."), f"{project['name']}.pages.dev".lower())
            for project in entries
            for domain in project["domains"]
        }
        for record in records:
            if not isinstance(record.get("id"), str) or not record["id"]:
                continue
            key = (
                str(record.get("name", "")).lower().rstrip("."),
                str(record.get("content", "")).lower(),
            )
            if str(record.get("type", "")).upper() == "CNAME" and key in targets:
                dns_records.append(record)
            else:
                dns_skipped += 1
    else:
        print_warn("  未配置 dns.zone_id，跳过 DNS 记录清理")

    return {"projects": entries, "kv": kv, "dns": dns_records, "dns_skipped": dns_skipped}


def _print_account_plan(account: Account, plan: dict) -> None:
    """打印单个账号的待删除清单，并标注非配置文件中的资源。"""
    domain_count = sum(len(project["domains"]) for project in plan["projects"])
    configured_project = account.pages.project_name
    configured_kv = account.pages.kv_namespace
    extra_projects = [p["name"] for p in plan["projects"] if p["name"] != configured_project]
    extra_kv = [ns for ns in plan["kv"] if ns.get("title") != configured_kv]

    print_info(
        f"  {account.name}：全部 Pages 项目 {len(plan['projects'])} 个 / "
        f"全部自定义域 {domain_count} 个 / 全部 KV 命名空间 {len(plan['kv'])} 个 / "
        f"本账号 DNS 记录 {len(plan['dns'])} 条"
    )
    if extra_projects:
        print_warn(f"    其中 {len(extra_projects)} 个非配置文件中的项目，将一并删除：" + "、".join(extra_projects))
    for project in plan["projects"]:
        domains = "、".join(project["domains"]) or "无"
        tag = "" if project["name"] == configured_project else "  ← 非配置文件中的项目"
        print_info(f"    - 项目 {project['name']}（自定义域：{domains}）{tag}")
    if extra_kv:
        print_warn(f"    其中 {len(extra_kv)} 个非配置文件中的 KV 命名空间，将一并删除（可能被其他 Worker 使用）")
    for ns in plan["kv"]:
        tag = "" if ns.get("title") == configured_kv else "  ← 非配置文件中的 KV 命名空间"
        print_info(f"    - KV 命名空间 {ns.get('title', ns['id'])}{tag}")
    for record in plan["dns"]:
        print_info(f"    - DNS {record.get('type', '')} {record.get('name', '')} -> {record.get('content', '')}")
    if plan.get("dns_skipped"):
        print_info(f"    Zone 内另有 {plan['dns_skipped']} 条记录不属于本账号项目，已保留")


def _delete_account_resources(api: CfApiClient, account: Account, plan: dict, proxy: str = "") -> bool:
    """删除清单内的全部资源；任一删除失败返回 False。"""
    ok = True

    for project in plan["projects"]:
        name = project["name"]
        for domain in project["domains"]:
            result = api.delete_domain(name, domain)
            if result and result.get("success"):
                print_ok(f"    已删除自定义域 {domain}")
            else:
                print_error(f"    删除自定义域失败：{domain}（{api.last_error or '未知原因'}）")
                ok = False
        result = api.delete_project(name)
        if result and result.get("success"):
            print_ok(f"  已删除 Pages 项目 {name}")
        else:
            print_error(f"  删除 Pages 项目失败：{name}（{api.last_error or '未知原因'}）")
            ok = False

    for ns in plan["kv"]:
        result = api.delete_kv_namespace(ns["id"])
        if result and result.get("success"):
            print_ok(f"  已删除 KV 命名空间 {ns.get('title', ns['id'])}")
        else:
            print_error(f"  删除 KV 命名空间失败：{ns.get('title', ns['id'])}（{api.last_error or '未知原因'}）")
            ok = False

    if plan["dns"]:
        with CfApiClient(account.account_id, account.dns.token, proxy=proxy) as dns_api:
            for record in plan["dns"]:
                result = dns_api.delete_dns_record(account.dns.zone_id, record["id"])
                if result and result.get("success"):
                    print_ok(f"  已删除 DNS 记录 {record.get('name', '')} -> {record.get('content', '')}")
                else:
                    print_error(f"  删除 DNS 记录失败：{record.get('name', '')}（{dns_api.last_error or '未知原因'}）")
                    ok = False

    return ok


def delete_workflow(cfg: Config) -> None:
    """完整删除流程入口：实时查询并删除账号下全部资源。"""
    accounts = get_enabled_accounts(cfg)
    if not accounts:
        print_error("没有已启用的账号（请检查 config.yaml 中的 enabled 字段）")
        wait_enter()
        return

    selected_accounts = select_accounts(accounts)
    if not selected_accounts:
        return

    print_header("批量删除")
    print_warn("删除范围：账号下全部资源，不限于配置文件中的那一个项目。")
    print_warn("同一账号下的其他 Pages 项目、其他 Worker 使用的 KV 命名空间、")
    print_warn("以及本账号项目已绑定自定义域对应的 DNS 记录都会被删除，且不可恢复。")
    print_warn("若该账号还承载其他业务，请先在待删除清单中核对。")
    print()
    print_info("将实时查询并删除每个账号下的全部资源（不读取配置中的项目名 / 域名 / KV 名）：")
    print_info("  1. 全部 Pages 项目（含其全部自定义域）")
    print_info("  2. 全部 KV 命名空间")
    print_info("  3. 本账号项目域名对应的 DNS 记录（需 dns.zone_id；其他记录保留）")
    print()

    # ========== 阶段一：实时查询 ==========
    print_info(">> 正在实时查询账号下全部资源 ...")
    plans: list[tuple[Account, dict]] = []
    failed: list[Account] = []
    for account in selected_accounts:
        try:
            with CfApiClient(account.account_id, account.token, proxy=cfg.proxy) as api:
                plan = _collect_account_resources(api, account, proxy=cfg.proxy)
        except Exception as exc:
            print_error(f"账号 '{account.name}' 查询异常：{exc}")
            plan = None
        if plan is None:
            failed.append(account)
        else:
            plans.append((account, plan))

    if not plans:
        print_ok("========== 删除完成 ==========")
        print_info(f"成功：0，失败：{len(failed)}")
        for account in failed:
            print_error(f"  失败账号：{account.name}")
        wait_enter()
        return

    # ========== 阶段二：确认清单 ==========
    print_header("待删除清单")
    for account, plan in plans:
        _print_account_plan(account, plan)
    print()
    print_warn("以上资源将被永久删除，无法恢复。")
    try:
        answer = input("确认删除账号下全部资源？输入 yes 继续：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "yes":
        print_warn("已取消，未删除任何资源")
        wait_enter()
        return

    # ========== 阶段三：执行删除 ==========
    for account, plan in plans:
        print_header(f"--- {account.name} ---")
        try:
            with CfApiClient(account.account_id, account.token, proxy=cfg.proxy) as api:
                if not _delete_account_resources(api, account, plan, proxy=cfg.proxy):
                    failed.append(account)
        except Exception as exc:
            print_error(f"账号 '{account.name}' 执行异常：{exc}")
            failed.append(account)

    print_ok("========== 删除完成 ==========")
    print_info(f"成功：{len(selected_accounts) - len(failed)}，失败：{len(failed)}")
    for account in failed:
        print_error(f"  失败账号：{account.name}")
    wait_enter()
