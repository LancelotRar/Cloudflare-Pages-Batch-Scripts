<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

# Cloudflare Pages Batch Scripts

<p align="center">
  <img src="https://count.getloli.com/@:LancelotRar?theme=booru-koe&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto">
</p>

---

多账号 Cloudflare Pages 批量部署/删除工具。从源码仓库下载 → 创建/更新 Pages 项目 → 配置环境变量 + KV 绑定 + 自定义域名 → 部署，全流程自动化。


## 功能

### 批量部署

对每个账号自动执行：

1. 通过 CF API 创建 Pages 项目（不存在时自动创建）
2. `wrangler pages deploy` 上传源码
3. 创建 KV 命名空间、设置环境变量、添加自定义域名
4. 配置生效后重新部署

### 批量删除

对每个账号自动执行：

1. 列出该账号下所有 Pages 项目
2. 灵活选择（`1,3` / `1-5` / `A` 全选）
3. 删除域名 → 清理旧部署 → 删除项目
4. 可选删除 KV 命名空间

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
| **Cloudflare API Token** | 需有 Pages 读写权限，在 [API Tokens](https://dash.cloudflare.com/profile/api-tokens) 创建 |

验证 wrangler 安装：

```powershell
wrangler --version
```

---

## 安装

```powershell
# 1. 克隆仓库
git clone https://github.com/<your-username>/Cloudflare-Pages-Batch-Scripts.git cf_pages_batch_scripts
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

编辑 `config.yaml`，填入你的 Cloudflare API Token 和 Pages 项目信息：

```yaml
accounts:
  - name: my-account
    enabled: true
    token: cfat_xxxxx            # 你的 Cloudflare API Token
    account_id: xxxxxx           # 你的账户 ID
    pages:
      project_name: my-project   # Pages 项目名
      domain: my-domain.com      # 自定义域名（可选）
      kv_namespace: my-kv        # KV 命名空间（可选）
```

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
│  1.  批量删除    查询 CF → 删除项目 + KV     │
│  2.  批量部署    创建/更新 Pages 项目         │
│  Q.  退出                                    │
└───────────────────────────────────────────────┘
```

也可直接指定虚拟环境中的 Python 运行（无需先激活）：

```powershell
.venv\Scripts\python -m cf_pages_batch_scripts
```

---

## 配置文件

编辑 `config.yaml`（从 `config.yaml.example` 复制而来）。示例中包含 5 个账号配置，覆盖不同场景：

```yaml
files_to_redeploy:
  dir: files-to-redeploy
  download_url: https://example.com/source.zip

accounts:
  # 账号 1 — 完整配置：自定义域名 + KV + 环境变量
  - name: my-account
    enabled: true
    token: cfat_xxxxxxxxxxxxxxxxxxxxxxxxxx1
    account_id: a1b2c3d4e5f6a1b2c3d4e5f6
    pages:
      project_name: my-project
      domain: my-domain.com
      kv_create: true
      kv_namespace: my-kv
      kv_binding: true
      kv_binding_env: KV
      project_type: production
    env:
      - name: UUID
        type: plain_text
        value: 550e8400-e29b-41d4-a716-446655440000

  # 账号 2 — 最小配置：仅必填，无域名无 KV
  - name: my-account
    enabled: true
    token: cfat_xxxxxxxxxxxxxxxxxxxxxxxxxx2
    account_id: b2c3d4e5f6a1b2c3d4e5f6a1
    pages:
      project_name: my-minimal-project

  # 账号 3 — preview 类型，创建 KV 但不绑定
  - name: my-account
    enabled: true
    token: cfat_xxxxxxxxxxxxxxxxxxxxxxxxxx3
    account_id: c3d4e5f6a1b2c3d4e5f6a1b2
    pages:
      project_name: my-preview-app
      kv_create: true
      kv_namespace: my-preview-kv
      kv_binding: false
      project_type: preview

  # 账号 4 — 禁用账号（被跳过）
  - name: my-account
    enabled: false
    token: cfat_xxxxxxxxxxxxxxxxxxxxxxxxxx4
    account_id: d4e5f6a1b2c3d4e5f6a1b2c3
    pages:
      project_name: my-disabled-project
      domain: disabled.example.com

  # 账号 5 — 多环境变量 + 自定义 KV 绑定名
  - name: my-account
    enabled: true
    token: cfat_xxxxxxxxxxxxxxxxxxxxxxxxxx5
    account_id: e5f6a1b2c3d4e5f6a1b2c3d4
    pages:
      project_name: my-env-rich-app
      domain: env-rich.example.com
      kv_create: true
      kv_namespace: my-rich-kv
      kv_binding: true
      kv_binding_env: MY_KV
      project_type: production
    env:
      - name: API_KEY
        type: secret_text
        value: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
      - name: DATABASE_URL
        type: plain_text
        value: postgres://user:pass@localhost:5432/db
      - name: DEBUG
        type: plain_text
        value: "false"
```

### 参数说明

| 路径 | 字段 | 说明 |
|---|---|---|
| `files_to_redeploy` | `dir` | 源码解压目录名 |
| | `download_url` | 部署源码 ZIP 下载地址 |
| `accounts[]` | `name` | 显示名称 |
| | `enabled` | `true`=启用，`false`=跳过 |
| | `token` | Cloudflare API Token |
| | `account_id` | Cloudflare 账户 ID |
| `accounts[].pages` | `project_name` | Pages 项目名称 |
| | `domain` | 自定义域名（为空则跳过） |
| | `kv_create` | 是否自动创建 KV 命名空间 |
| | `kv_namespace` | KV 命名空间标题 |
| | `kv_binding` | 是否将 KV 绑定到项目 |
| | `kv_binding_env` | KV 绑定的环境变量名，默认 `KV` |
| | `project_type` | `production` 或 `preview` |
| `accounts[].env[]` | `name` | 环境变量名 |
| | `type` | `plain_text` 或 `secret_text` |
| | `value` | 环境变量值 |

### 多账号支持

`accounts` 支持配置多个账号，`enabled: false` 的账号会被自动跳过。

---

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

---

## 开发

```powershell
# 激活虚拟环境
.venv\Scripts\activate

# 安装测试依赖
pip install pytest pytest-httpx

# 运行全部测试
python -m pytest tests/ -v

# 代码检查（需安装 ruff）
pip install ruff
ruff check cf_pages_batch_scripts/
```

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
├── tests/                 # pytest 测试（80+ 用例）
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_api.py
│   └── test_workflows.py
├── config.yaml.example    # 配置文件模板
├── config.yaml            # 用户配置（已 gitignore）
├── pyproject.toml         # 项目元数据与依赖声明
└── README.md
```

---

## 许可证

MIT
