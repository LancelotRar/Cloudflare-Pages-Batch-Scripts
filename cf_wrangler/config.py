from pathlib import Path

import yaml

from .models import Account, Config, EnvVar, FilesToRedeploy, PagesConfig


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
    fr = raw.get("files_to_redeploy", {}) if raw else {}
    return FilesToRedeploy(
        dir=_get_str(fr, "dir", "files-to-redeploy"),
        download_url=_get_str(fr, "download_url"),
    )


def _parse_env_vars(raw_acct: dict) -> list[EnvVar]:
    """从原始 YAML dict 解析环境变量列表。"""
    return [
        EnvVar(name=_get_str(ev, "name"), var_type=_get_str(ev, "type"), value=_get_str(ev, "value"))
        for ev in raw_acct.get("env", [])
    ]


def _parse_pages_config(raw_pages: dict) -> PagesConfig:
    """从原始 YAML dict 解析 PagesConfig。"""
    return PagesConfig(
        project_name=_get_str(raw_pages, "project_name"),
        domain=_get_str(raw_pages, "domain"),
        kv_create=_get_bool(raw_pages, "kv_create"),
        kv_namespace=_get_str(raw_pages, "kv_namespace"),
        kv_binding=_get_bool(raw_pages, "kv_binding"),
        kv_binding_env=_get_str(raw_pages, "kv_binding_env", "KV"),
        project_type=_get_str(raw_pages, "project_type", "production"),
    )


def _parse_accounts(raw: dict | None) -> list[Account]:
    """从原始 YAML dict 解析账号列表。"""
    accounts: list[Account] = []
    for raw_acct in (raw.get("accounts", []) if raw else []):
        accounts.append(Account(
            name=_get_str(raw_acct, "name"),
            enabled=_get_bool(raw_acct, "enabled"),
            token=_get_str(raw_acct, "token"),
            account_id=_get_str(raw_acct, "account_id"),
            pages=_parse_pages_config(raw_acct.get("pages", {})),
            env=_parse_env_vars(raw_acct),
        ))
    return accounts


def load_config(path: Path | None = None) -> Config:
    """加载并解析 config.yaml"""
    if path is None:
        path = find_config()

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return Config(
        files_to_redeploy=_parse_files_to_redeploy(raw),
        accounts=_parse_accounts(raw),
    )


def get_enabled_accounts(cfg: Config) -> list[Account]:
    """返回已启用且配置完整的账号列表"""
    return [
        a for a in cfg.accounts
        if a.enabled and a.token and a.account_id and a.pages.project_name
    ]
