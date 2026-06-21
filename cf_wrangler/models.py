from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvVar:
    """环境变量，如 UUID/ADMIN 等"""
    name: str
    var_type: str  # plain_text 或 secret_text
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PagesConfig:
    """单个 Pages 项目的配置"""
    project_name: str
    domain: str = ""
    kv_create: bool = False
    kv_namespace: str = ""
    kv_binding: bool = False
    kv_binding_env: str = "KV"
    project_type: str = "production"


@dataclass(frozen=True, slots=True, kw_only=True)
class Account:
    """一个 Cloudflare 账号下的 Pages 项目配置"""
    name: str
    enabled: bool
    token: str = field(repr=False)  # 敏感字段，不在 repr 中泄露
    account_id: str
    pages: PagesConfig
    env: list[EnvVar] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class FilesToRedeploy:
    """全局配置：重新部署所需文件"""
    dir: str = "files-to-redeploy"
    download_url: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """顶层配置文件"""
    files_to_redeploy: FilesToRedeploy = field(default_factory=FilesToRedeploy)
    accounts: list[Account] = field(default_factory=list)
