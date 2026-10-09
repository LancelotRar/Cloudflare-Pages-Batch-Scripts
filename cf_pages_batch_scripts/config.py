from pathlib import Path

import yaml

from .models import Account, Config, DnsConfig, EnvVar, FilesToRedeploy, PagesConfig


def _get_str(d: dict, key: str, default: str = "") -> str:
    """Safely extract a string value from a dict."""
    v = d.get(key, default)
    return str(v) if v is not None else default


def _get_bool(d: dict, key: str, default: bool = False) -> bool:
    """Safely extract a boolean value from a dict."""
    v = d.get(key, default)
    return bool(v) if v is not None else default


def find_config() -> Path:
    """查找 config.yaml，优先当前工作目录，其次项目根目录"""
    candidates = [
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parent.parent / "config.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "config.yaml not found. Please ensure the file exists in the project root or current directory."
    )


def _parse_files_to_redeploy(raw: dict | None) -> FilesToRedeploy:
    """从原始 YAML dict 解析 FilesToRedeploy。"""
    fr = (raw.get("files_to_redeploy") or {}) if raw else {}
    return FilesToRedeploy(
        dir=_get_str(fr, "dir", "files-to-redeploy"),
        download_url=_get_str(fr, "download_url"),
    )


def _parse_env_vars(raw_pages: dict) -> list[EnvVar]:
    """从原始 YAML dict 解析环境变量列表。"""
    raw_env = raw_pages.get("env") or []
    if not isinstance(raw_env, list):
        raise ValueError(f"pages.env 必须是列表，当前为 {type(raw_env).__name__}")
    envs: list[EnvVar] = []
    for index, ev in enumerate(raw_env, start=1):
        if not isinstance(ev, dict):
            raise ValueError(f"pages.env 第 {index} 项必须是映射（name/type/value），当前为 {ev!r}")
        envs.append(EnvVar(name=_get_str(ev, "name"), var_type=_get_str(ev, "type"), value=_get_str(ev, "value")))
    return envs


def _parse_pages_config(raw_pages: dict) -> PagesConfig:
    """从原始 YAML dict 解析 PagesConfig。"""
    project_type = _get_str(raw_pages, "project_type", "production")
    if project_type not in {"production", "preview"}:
        raise ValueError(f"pages.project_type 配置无效：{project_type!r}（应为 'production' 或 'preview'）")
    return PagesConfig(
        project_name=_get_str(raw_pages, "project_name"),
        domain=_get_str(raw_pages, "domain"),
        kv_create=_get_bool(raw_pages, "kv_create"),
        kv_namespace=_get_str(raw_pages, "kv_namespace"),
        kv_binding=_get_bool(raw_pages, "kv_binding"),
        kv_binding_env=_get_str(raw_pages, "kv_binding_env"),
        project_type=project_type,
        env=_parse_env_vars(raw_pages),
    )


def _parse_dns_config(raw_dns: dict) -> DnsConfig:
    """从原始 YAML dict 解析 DnsConfig。"""
    raw_ttl = raw_dns.get("ttl", 1)
    if str(raw_ttl).lower() == "auto":
        ttl = 1
    else:
        try:
            ttl = int(raw_ttl)
        except (TypeError, ValueError):
            raise ValueError(f"dns.ttl 配置无效：{raw_ttl!r}（应为 'auto' 或正整数秒）") from None
    return DnsConfig(
        token=_get_str(raw_dns, "dns_token"),
        zone_id=_get_str(raw_dns, "zone_id"),
        record_type=_get_str(raw_dns, "type", "CNAME").upper(),
        content=_get_str(raw_dns, "content"),
        proxied=_get_bool(raw_dns, "proxied"),
        ttl=ttl,
    )


def _parse_accounts(raw: dict | None) -> list[Account]:
    """从原始 YAML dict 解析账号列表。"""
    raw_accounts = (raw.get("accounts") or []) if raw else []
    if not isinstance(raw_accounts, list):
        raise ValueError(f"accounts 必须是列表，当前为 {type(raw_accounts).__name__}")
    accounts: list[Account] = []
    for index, raw_acct in enumerate(raw_accounts, start=1):
        if not isinstance(raw_acct, dict):
            raise ValueError(f"accounts 第 {index} 项必须是映射，当前为 {raw_acct!r}")
        accounts.append(Account(
            name=_get_str(raw_acct, "name"),
            enabled=_get_bool(raw_acct, "enabled"),
            token=_get_str(raw_acct, "token"),
            account_id=_get_str(raw_acct, "account_id"),
            pages=_parse_pages_config(raw_acct.get("pages") or {}),
            dns=_parse_dns_config(raw_acct.get("dns") or {}),
        ))
    return accounts


def load_config(path: Path | None = None) -> Config:
    """加载并解析 config.yaml"""
    if path is None:
        path = find_config()

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is not None and not isinstance(raw, dict):
        raise ValueError(f"配置文件 {path} 顶层必须是 key: value 映射，当前为 {type(raw).__name__}")

    return Config(
        files_to_redeploy=_parse_files_to_redeploy(raw),
        proxy=_get_str(raw or {}, "proxy"),
        accounts=_parse_accounts(raw),
    )


def get_enabled_accounts(cfg: Config) -> list[Account]:
    """返回已启用且配置完整的账号列表"""
    return [
        a for a in cfg.accounts
        if a.enabled and a.token and a.account_id and a.pages.project_name
    ]
