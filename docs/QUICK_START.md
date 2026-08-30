# 快速开始

本文只描述本地开发。生产入口、身份密钥、数据库地址和代理链路由仓库外受限配置管理。

## 1. 准备环境

Windows 下统一在 Git Bash 中执行：

```bash
cd /f/Project/Shadow/shadow-foliant
python -m venv .venv
source .venv/Scripts/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

运行时使用 PostgreSQL，不再支持 SQLite、Streamlit 或 Docker 启动方式。按需在未提交的
`.env` 中配置 PostgreSQL、Shadow Identity、LLM 和行情源；密钥不得写入仓库或日志。

## 2. 初始化或迁移数据库

```bash
bash scripts/migrate.sh
```

迁移脚本可独立运行，使用 `PG_HOST`、`PG_PORT`、`PG_DATABASE`、`PG_USER`、
`PG_PASSWORD`。空库会先执行幂等初始化，已有库只执行尚未登记的版本化迁移。

## 3. 启动开发服务

```bash
python webui/run_dev.py
```

本地默认地址为 `http://localhost:8601`。Web 登录使用 OIDC Authorization Code + PKCE；
`stock-users` 可查看普通页面，涉及持仓、成交、环境配置和任务控制的敏感能力要求
`stock-admins`。

后台任务另开终端运行：

```bash
source .venv/Scripts/activate
python -m jobs.jobs_hub --serve
```

本地 Agent 可使用 stdio MCP：

```bash
python mcp_server.py
```

远程 MCP、HTTP Agent 和外部机器调用必须携带面向 `foliant` audience 的 Agent Bearer；
浏览器 Session Cookie 不能替代 Agent 身份。

## 4. 健康检查

- `/healthz`：公开、无状态，不访问数据库。
- `/readyz`：受保护的运维就绪检查。
- `/api/health`：仅保留安全健康语义。

## 5. 常见问题

- Web 无法登录：核对 issuer、client ID、回调地址、系统时间和仓库外 client secret。
- 页面能开但敏感操作返回 403：确认当前用户属于 `stock-admins`。
- 手动任务一直排队：确认 `jobs_hub` 常驻并能访问 PostgreSQL。
- 行情不可用：查看数据质量和源健康状态；不要并发猛拉第三方接口。
- PDF 中文乱码：在运行主机安装 Noto CJK 或文泉驿字体。

更完整的能力说明见 [使用文档](../使用文档.md) 和
[Shadow 插件接入](SHADOW_PLUGIN_INTEGRATION.md)。
