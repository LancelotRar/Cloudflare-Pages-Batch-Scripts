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
    domain: str = ""  # 留空 = 清理模式：解绑项目自定义域并删除指向本项目的 DNS 记录
    kv_create: bool = False
    kv_namespace: str = ""
    kv_binding: bool = False
    kv_binding_env: str = ""
    project_type: str = "production"
    env: list[EnvVar] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class DnsConfig:
    """单个 DNS 记录配置。记录名与 pages.domain 同源，不再单独配置。"""

    token: str = field(default="", repr=False)
    zone_id: str = ""
    record_type: str = "CNAME"
    content: str = ""  # 可选；为空时按 <pages.project_name>.pages.dev 拼接
    proxied: bool = False
    ttl: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class Account:
    """一个 Cloudflare 账号下的 Pages 项目配置"""

    name: str
    enabled: bool
    token: str = field(repr=False)  # 敏感字段，不在 repr 中泄露
    account_id: str
    pages: PagesConfig
    dns: DnsConfig = field(default_factory=DnsConfig)

    @property
    def dns_content(self) -> str:
        """DNS 记录目标值：创建/修改时写入记录，`domain` 留空时作为清理匹配条件。

        未显式配置 dns.content 时按 Pages 默认域名拼接。
        """
        if self.dns.content:
            return self.dns.content
        return f"{self.pages.project_name}.pages.dev" if self.pages.project_name else ""


@dataclass(frozen=True, slots=True, kw_only=True)
class FilesToRedeploy:
    """全局配置：部署所需源码"""

    dir: str = "files-to-redeploy"
    download_url: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """顶层配置文件"""

    files_to_redeploy: FilesToRedeploy = field(default_factory=FilesToRedeploy)
    proxy: str = ""  # 可选代理，如 http://127.0.0.1:7890；为空时信任环境变量代理
    accounts: list[Account] = field(default_factory=list)
