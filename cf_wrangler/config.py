from pathlib import Path
import yaml

from .models import Config, Account, PagesConfig, EnvVar, FilesToRedeploy


def find_config() -> Path:
    """查找 config.yaml，优先脚本所在目录，其次当前工作目录"""
    script_dir = Path(__file__).resolve().parent.parent
    candidates = [
        script_dir / "config.yaml",
        Path.cwd() / "config.yaml",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("config.yaml not found. Please ensure the file exists in the project root or current directory.")


def load_config(path: Path | None = None) -> Config:
    """加载并解析 config.yaml"""
    if path is None:
        path = find_config()
    
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    
    fr = raw.get("files_to_redeploy", {}) if raw else {}
    files_to_redeploy = FilesToRedeploy(
        dir=fr.get("dir", "files-to-redeploy"),
        download_url=fr.get("download_url", ""),
    )
    
    accounts: list[Account] = []
    raw_accounts = raw.get("accounts", []) if raw else []
    for raw_acct in raw_accounts:
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
                var_type=raw_env.get("type", ""),
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
    """返回已启用且配置完整的账号列表"""
    if cfg is None:
        cfg = load_config()
    return [
        a for a in cfg.accounts
        if a.enabled and a.token and a.account_id and a.pages.project_name
    ]
