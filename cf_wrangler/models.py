from dataclasses import dataclass, field


@dataclass
class EnvVar:
    """环境变量，如 UUID/ADMIN 等"""
    name: str
    type: str
    value: str


@dataclass
class PagesConfig:
    """单个 Pages 项目的配置"""
    project_name: str
    domain: str = ""
    kv_create: bool = False
    kv_namespace: str = ""
    kv_binding: bool = False
    kv_binding_env: str = "KV"
    project_type: str = "production"


@dataclass
class Account:
    """一个 Cloudflare 账号下的 Pages 项目配置"""
    name: str
    enabled: bool
    token: str
    account_id: str
    pages: PagesConfig
    env: list[EnvVar] = field(default_factory=list)


@dataclass
class FilesToRedeploy:
    """全局配置：重新部署所需文件"""
    dir: str = "files-to-redeploy"
    download_url: str = ""


@dataclass
class Config:
    """顶层配置文件"""
    files_to_redeploy: FilesToRedeploy = field(default_factory=FilesToRedeploy)
    accounts: list[Account] = field(default_factory=list)
