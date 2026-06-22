# Cloudflare Pages Manager

多账号 Cloudflare Pages 批量部署/删除工具。支持从源码仓库下载 → 创建/更新 Pages 项目 → 配置环境变量 + KV 绑定 + 自定义域名 → 部署的双上传工作流。

## 目录

- [前置要求](#前置要求)
- [安装](#安装)
- [配置文件](#配置文件)
- [使用方法](#使用方法)
- [工作流说明](#工作流说明)
  - [批量部署](#批量部署)
  - [批量删除](#批量删除)
- [配置参考](#配置参考)
- [项目结构](#项目结构)

## 前置要求

| 依赖 | 说明 |
|---|---|
| **Node.js** (LTS) | wrangler CLI 运行环境，[下载](https://nodejs.org/) |
| **wrangler CLI** | Cloudflare 官方 CLI，`npm install -g wrangler` |
| **Python 3.10+** | Python 版本运行环境 |
| **Cloudflare API Token** | 需有 Pages 读写权限，在 [API Tokens](https://dash.cloudflare.com/profile/api-tokens) 创建 |

验证 wrangler 安装：
```powershell
wrangler --version
```

## 安装

```powershell
# 1. 克隆项目
git clone <repo-url>
cd CF-wrangler

# 2. 安装项目（自动装好依赖 + 注册 cf-pages-batch-deploy 全局命令）
pip install .

# 3. 初始化配置文件（从模板复制）
cp config.yaml.example config.yaml
# Windows: copy config.yaml.example config.yaml
```

> 开发模式用 `pip install -e .`，改代码不需要重装。

## 配置文件

编辑 `config.yaml`（从 `config.yaml.example` 复制而来），配置你的 Cloudflare 账号和 Pages 项目。

> ⚠️ `config.yaml` 包含 API Token 等敏感信息，已被加入 `.gitignore`，不会提交到 git 仓库。
> 首次使用请从 `config.yaml.example` 复制一份。

```yaml
files_to_redeploy:
  dir: files-to-redeploy
  download_url: https://example.com/source.zip

accounts:
  - name: my-account       # 显示名称
    enabled: true           # true=启用，false=跳过
    token: cfat_xxx         # Cloudflare API Token
    account_id: xxxxxx      # 账户 ID
    pages:
      project_name: my-project   # Pages 项目名
      domain: my-domain.com      # 自定义域名（可选）
      kv_create: true            # 是否自动创建 KV 命名空间
      kv_namespace: my-kv        # KV 命名空间名称
      kv_binding: true           # 是否绑定 KV 命名空间到项目
      kv_binding_env: KV         # KV 绑定的环境变量名
      project_type: production   # production 或 preview
    env:
      - name: UUID
        type: plain_text         # plain_text 或 secret_text
        value: xxxxxxxx
      - name: ADMIN
        type: plain_text
        value: admin
```

- 支持多个账号，`enabled: false` 的账号会被跳过
- `download_url` 指向一个包含部署源码的 ZIP 包 URL

## 使用方法

```powershell
python -m cf_wrangler
```

交互菜单：
```
1. 批量删除    查询 CF → 删除自定义域 + 项目 + KV
2. 批量部署    创建/更新 Pages 项目并上传源码
Q. 退出
```

## 工作流说明

### 批量部署

对每个选中的账号依次执行：

1. **检查/创建项目** — 通过 CF API 创建 Pages 项目（若不存在）
2. **首次上传** — `wrangler pages deploy` 部署源码
3. **配置项目** — 创建 KV 命名空间 → 设置环境变量 + KV 绑定 → 添加自定义域名
4. **二次上传** — 配置生效后重新部署

> 为什么需要二次上传？Cloudflare Pages 的项目配置（环境变量、KV 绑定、域名）在首次部署后设置，需要再次部署让配置生效。

### 批量删除

对每个选中的账号依次执行：

1. 列出该账号下所有 Pages 项目
2. 选择要删除的项目（支持 `1,3` / `1-5` / `A` 全选）
3. 删除自定义域名 → 清理旧部署（超过 50 个时提示）→ 删除项目
4. 可选删除 KV 命名空间（会标注哪些仍绑定到项目）

## 配置参考

### config.yaml 字段说明

| 路径 | 字段 | 类型 | 说明 |
|---|---|---|---|
| `files_to_redeploy` | `dir` | string | 源码解压目录名 |
| | `download_url` | string | 部署源码 ZIP 下载地址 |
| `accounts[]` | `name` | string | 显示名称 |
| | `enabled` | bool | 是否启用 |
| | `token` | string | Cloudflare API Token |
| | `account_id` | string | Cloudflare 账户 ID |
| `accounts[].pages` | `project_name` | string | Pages 项目名称 |
| | `domain` | string | 自定义域名（为空则跳过） |
| | `kv_create` | bool | 是否自动创建 KV 命名空间 |
| | `kv_namespace` | string | KV 命名空间标题 |
| | `kv_binding` | bool | 是否将 KV 绑定到项目 |
| | `kv_binding_env` | string | KV 绑定的环境变量名，默认 `KV` |
| | `project_type` | string | `production` 或 `preview` |
| `accounts[].env[]` | `name` | string | 环境变量名 |
| | `type` | string | `plain_text` 或 `secret_text` |
| | `value` | string | 环境变量值 |

## 项目结构

```
CF-wrangler/
├── cf_wrangler/           # Python 包
│   ├── __init__.py        # 包入口，导出 main()
│   ├── __main__.py        # 入口，菜单循环
│   ├── models.py          # 数据类（Account, PagesConfig, EnvVar）
│   ├── config.py          # 加载解析 config.yaml
│   ├── api.py             # Cloudflare REST API 客户端（含重试逻辑）
│   ├── ui.py              # 交互界面（Rich 彩色输出）
│   └── workflows.py       # 部署/删除工作流逻辑
├── tests/                 # 测试目录（pytest, 80 个测试用例）
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_api.py
│   └── test_workflows.py
├── config.yaml.example    # 配置文件模板（复制为 config.yaml 后使用）
├── config.yaml            # 用户配置文件（已加入 .gitignore）
├── pyproject.toml         # Python 项目配置（构建/lint/typecheck/测试）
└── README.md
```

## 开发

```powershell
# 安装测试依赖
pip install pytest pytest-httpx

# 运行全部测试
python -m pytest tests/ -v

# 代码检查（需安装 ruff）
pip install ruff
ruff check cf_wrangler/
```
