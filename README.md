<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

# Cloudflare Pages Batch Scripts

<p align="center">
  <img src="https://count.getloli.com/@:cpbs?theme=booru-koe&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto">
</p>

---

多账号 Cloudflare Pages 批量部署/删除工具。从 ZIP 下载源码 → 创建 Pages 项目 → wrangler 上传 → 配置环境变量 + KV 绑定 + 自定义域名 → 重新部署 → 同步 DNS 记录，全流程自动化。

> [!TIP]  
> 可搭配 [edgetunnel项目](https://github.com/cmliu/edgetunnel) 实现快速批量部署、批量重建多Cloudflare账号下的Pages项目，实现请求数叠加，达到负载均衡效果，请求数限制突破单账户10w；并通过重建自定义域实现断流管控。[请求数叠加教程。](https://blog.cmliussss.com/p/edt2)

## 功能

### 批量部署

对每个账号自动执行：

1. 通过 CF API 创建 Pages 项目（已存在则跳过）
2. `wrangler pages deploy` 上传源码
3. 配置项目：创建/查找 KV 命名空间 → 设置环境变量 + KV 绑定 → 同步自定义域名
4. `wrangler pages deploy` 重新部署使配置生效
5. 查询并同步 DNS 记录（失败时当前账号标记失败，继续后续账号）

### 批量删除

对每个账号自动执行：

1. 选择要操作的账号
2. 按配置删除自定义域名
3. 按配置删除 Pages 项目
4. 按配置删除 KV 命名空间（需查询 ID）

### 特性

- 多账号支持，单次操作全量处理
- 交互式菜单，无需记忆命令行参数
- 自动重试 API 请求（5xx/429 自动退避重试）
- 完善的错误提示

---

## 前置要求

| 依赖 | 说明 |
|---|---|
| **Python 3.10+** | 运行环境 |
| **Node.js** (LTS) | wrangler CLI 运行环境，[下载](https://nodejs.org/) |
| **wrangler CLI** | Cloudflare 官方 CLI，`npm install -g wrangler` |
| **Pages API Token** | 每个 Pages 项目所在账户的 API Token，需有 Pages 读写权限 |
| **DNS API Token** | 托管目标域名账户的独立 API Token，需对目标 Zone 具有 `DNS Write` 权限 |

验证 wrangler 安装：

```powershell
wrangler --version
```

---

## 安装

```powershell
# 1. 克隆仓库
git clone https://github.com/LancelotRar/Cloudflare-Pages-Batch-Scripts.git cf_pages_batch_scripts
cd cf_pages_batch_scripts

# 2. 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 3. 安装项目（自动安装 pyyaml、httpx、rich 依赖）
pip install .

# 4. 从模板创建配置文件
copy config.yaml.example config.yaml
```

> 开发模式用 `pip install -e .`，改代码不需要重装。

---

## 快速开始

### 1. 编辑配置

编辑 `config.yaml`，分别填写 Pages 项目账户凭据和域名托管账户的 DNS 凭据：

见下方配置示例

### 2. 运行

先激活虚拟环境，再执行命令：

```powershell
# 激活虚拟环境（Windows）
.venv\Scripts\activate

# 激活虚拟环境（macOS / Linux）
# source .venv/bin/activate

cf_pages_batch_scripts
```

显示交互菜单：

```
┌────── Cloudflare Pages Batch Scripts ───────┐
│  1.  批量部署                                │
│  2.  批量删除                                │
│  Q.  退出                                    │
└───────────────────────────────────────────────┘
```

选择 `1`（批量部署）或 `2`（批量删除）后，显示账号列表：

```
┌────────── 账号列表 ──────────┐
│ #  名称        项目          域名            │
│ 1  my-account  my-project    my-domain.com   │
│ 2  my-account  my-minimal-project            │
└───────────────────────────────────────────────┘

[A]ll 全部账号
# 输入序号（如 1 或 1,3,5）选择单个或多个账号
[Q]uit 退出
```

输入 `A` 全选、`1,3,5` 选择多个账号，或 `Q` 退出。

也可直接指定虚拟环境中的 Python 运行（无需先激活）：

```powershell
.venv\Scripts\python -m cf_pages_batch_scripts
```

指定配置文件路径：

```powershell
cf_pages_batch_scripts -c /path/to/config.yaml
# 或
.venv\Scripts\python -m cf_pages_batch_scripts --config custom.yaml
```

不传 `-c` 时默认查找当前目录或项目根目录下的 `config.yaml`。

---

## 配置文件

编辑 `config.yaml`（从 `config.yaml.example` 复制而来）。示例中包含 4 个账号配置。

`accounts[].token` 和 `accounts[].account_id` 属于各自 Pages 项目所在的 Cloudflare 账户。`accounts[].dns.dns_token` 和 `accounts[].dns.zone_id` 是独立配置，必须属于实际托管目标域名的 Cloudflare 账户，而不是当前 Pages 项目账户。多个 Pages 账号使用同一个托管域名时，可以通过 `MyDNS` YAML 锚点共享同一组 `dns_token` 和 `zone_id`。

```yaml
Myenv: &Myenv [{name: UUID, type: plain_text, value: 550e8400-e28b-41d4-a716-446655440000}, {name: ADMIN, type: plain_text, value: your-password}]
Mypageskvyes: &Mypageskvyes {kv_create: true, kv_binding: true, kv_binding_env: KV, project_type: production}
Mypageskvno: &Mypageskvno {kv_create: false, kv_binding: false, project_type: production}
MyDNS: &MyDNS {dns_token: cfat_0000000000000000000000000000000000000000, zone_id: 00000000000000000000000000000000, type: CNAME, proxied: false, ttl: auto}

files_to_redeploy:
  dir: files-to-redeploy
  download_url: https://example.com/source.zip

accounts:
  - name: account-01
    enabled: false
    token: cfat_0000000000000000000000000000000000000001
    account_id: 00000000000000000000000000000001
    pages:
      <<: *Mypageskvyes
      project_name: my-project-01
      domain: my-project-01.example.com
      kv_namespace: my-project-01
      env: *Myenv
    dns:
      <<: *MyDNS
      name: my-project-01.example.com
      content: my-project-01.pages.dev

  - name: account-02
    enabled: false
    token: cfat_0000000000000000000000000000000000000002
    account_id: 00000000000000000000000000000002
    pages:
      <<: *Mypageskvyes
      project_name: my-project-02
      domain: my-project-02.example.com
      kv_namespace: my-project-02
      env: *Myenv
    dns:
      <<: *MyDNS
      name: my-project-02.example.com
      content: my-project-02.pages.dev

  - name: account-03
    enabled: true
    token: cfat_0000000000000000000000000000000000000003
    account_id: 00000000000000000000000000000003
    pages:
      <<: *Mypageskvno
      project_name: my-project-03
      domain: my-project-03.example.com
      env: *Myenv
    dns:
      <<: *MyDNS
      name: my-project-03.example.com
      content: my-project-03.pages.dev

  - name: account-04
    enabled: true
    token: cfat_0000000000000000000000000000000000000004
    account_id: 00000000000000000000000000000004
    pages:
      <<: *Mypageskvno
      project_name: my-project-04
      domain: my-project-04.example.com
      env: *Myenv
    dns:
      <<: *MyDNS
      name: my-project-04.example.com
      content: my-project-04.pages.dev

```

### 参数说明

| 路径 | 字段 | 说明 |
|---|---|---|
| `files_to_redeploy` | `dir` | 源码解压目录名 |
| | `download_url` | 部署源码 ZIP 下载地址 |
| `accounts[]` | `name` | 显示名称 |
| | `enabled` | `true`=启用，`false`=跳过 |
| | `token` | 当前 Pages 项目所在账户的 API Token |
| | `account_id` | 当前 Pages 项目所在账户的 Account ID |
| `accounts[].pages` | `project_name` | Pages 项目名称 |
| | `domain` | 目标自定义域名；部署时删除项目中其他自定义域名后添加该域名（为空则跳过） |
| | `kv_create` | 是否自动创建 KV 命名空间 |
| | `kv_namespace` | KV 命名空间标题 |
| | `kv_binding` | 是否将 KV 绑定到项目 |
| | `kv_binding_env` | KV 绑定的环境变量名，不填则绑定名为空 |
| | `project_type` | `production` 或 `preview` |
| `accounts[].pages.env[]` | `name` | 环境变量名 |
| | `type` | `plain_text` 或 `secret_text` |
| | `value` | 环境变量值 |
| `accounts[].dns` | `zone_id` | 托管目标域名账户中的 Zone ID，与当前 Pages 账户的 `account_id` 无关 |
| | `dns_token` | 托管目标域名账户的独立 API Token，需对目标 Zone 具有 `DNS Write` 权限；不会使用当前 Pages 账户的 `token` |
| | `type` | DNS 记录类型，默认 `CNAME` |
| | `name` | 完整 DNS 记录名称 |
| | `content` | DNS 记录目标值 |
| | `proxied` | 是否启用 Cloudflare 代理 |
| | `ttl` | TTL 秒数；`auto` 表示自动（API 值 `1`） |

### 多账号支持

`accounts` 支持配置多个账号，`enabled: false` 的账号会被自动跳过。

---

## 工作流说明

### 批量部署

对每个选中的账号依次执行：

1. **创建项目** — 通过 CF API 创建 Pages 项目（已存在则跳过）
2. **上传部署** — `wrangler pages deploy` 上传源码
3. **配置项目** — 创建/查找 KV 命名空间 → 设置环境变量 + KV 绑定 → 同步自定义域名
4. **重新部署** — 再次 `wrangler pages deploy` 使配置生效
5. **同步 DNS** — 项目部署成功后查询并创建或修改 DNS 记录

> 为什么需要重新部署？Cloudflare Pages 的项目配置（环境变量、KV 绑定、域名）在首次部署后设置，需要再次部署让配置生效。

配置了 `domain` 时，脚本会先查询项目当前绑定的自定义域名，删除所有与目标域名不同的旧域名，再添加目标域名。目标域名已经存在时不会重复添加；查询、删除或添加失败时会停止该账号的部署，避免继续执行最终重部署。

非空 `pages.env` 是受管理环境的完整白名单，脚本会添加或修改目标变量，并删除未声明的多余变量。KV 字段存在时会严格收敛绑定：`kv_binding: true` 只保留目标绑定，`kv_binding: false` 删除现有 KV 绑定。环境变量为空、KV 字段完全缺省或 `domain` 为空时，对应配置不进行任何操作。

配置完整的 `dns` 时，脚本使用域名托管账户的 `dns_token` 和 `zone_id` 建立独立 DNS API 请求，不使用 Pages 项目账户的 `token` 或 `account_id`。随后通过 Cloudflare API 的 `content.exact` 查询参数精确匹配 DNS 记录。找到记录后会将其名称、类型、代理状态和 TTL 修改为声明配置；记录不存在时创建，重复内容的记录会收敛为一条。DNS 查询、创建或修改失败会显示 Cloudflare API 错误详情并使当前账号失败，但不会影响后续账号继续部署。

### 批量删除

对每个选中的账号依次执行：

1. 选择要操作的账号
2. 按 `config.yaml` 配置删除自定义域名
3. 按配置删除 Pages 项目
4. 按配置删除 KV 命名空间（需查询 ID）

> 删除流程以 `config.yaml` 为唯一来源，不查询 CF 上实际存在的项目列表。

---

## 项目结构

```
cf_pages_batch_scripts/
├── cf_pages_batch_scripts/          # Python 包
│   ├── __init__.py        # 包入口
│   ├── __main__.py        # 命令行入口
│   ├── models.py          # 数据类（Account, PagesConfig, EnvVar）
│   ├── config.py          # YAML 配置加载与解析
│   ├── api.py             # Cloudflare REST API 客户端
│   ├── ui.py              # 交互界面（基于 Rich）
│   └── workflows.py       # 部署/删除工作流逻辑
├── config.yaml.example    # 配置文件模板
├── pyproject.toml         # 项目元数据与依赖声明
└── README.md
```

---

## 许可证

MIT
