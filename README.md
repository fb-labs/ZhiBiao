# 智标（ZhiBiao）

全流程智能招投标平台：上传招标文件，依次完成**招标解读 → 大纲规划 → 正文编写 → 质量检查 → 文档交付**。
页面与 API 由同一个 FastAPI 进程在同一端口提供（纯 B/S，无 Nginx、无反向代理）。

## 功能

| 环节 | 能力 |
|---|---|
| 招标解读 | 解析 PDF / DOCX / TXT，提炼项目概况、8 项关键维度、评分办法、实质性要求与风险项 |
| 大纲规划 | 依据解读结果生成两级章节结构，标注写作意图与目标字数 |
| 标书编写 | 按章节流式生成正文（SSE），支持手工编辑与逐章保存 |
| 质量检查 | 规则检查（完整性、字数、响应点、截止时间）+ AI 评标视角审查，输出问题清单、评分与裁定 |
| 修订闭环 | 针对问题生成修订稿，预览后一键应用到对应章节 |
| 文档交付 | 生成 DOCX / PDF，记录交付历史并支持下载 |
| 平台管理 | 账号与角色（RBAC）、模型服务、助手配置、文档模板、审计日志、统计看板 |

## 技术架构

- **后端**：Python 3.12+ / FastAPI / SQLAlchemy 2.0 异步 ORM / Pydantic v2
- **数据库**：PostgreSQL 16（默认）、MySQL 8、SQLite（快速体验）
- **模型**：任意 OpenAI 兼容接口；429 自动退避重试
- **前端**：React 19 / TypeScript 5 / Vite 7 / React Router 7 / Three.js
- **文档**：python-docx、pdfplumber、reportlab

## 目录结构

```text
zhibiao/                   独立后端
  api/                     接口：认证、项目、文件、解读、大纲、编写、检查、交付、账号、模型服务、模板、统计
  prompts.py               业务提示词
  models.py                数据模型（zb_ 前缀，21 张表）
  domain.py                状态、角色与权限定义
  llm.py                   OpenAI 兼容客户端（429 无限退避）
  files.py                 文件保存与文本解析
  app.py                   应用入口（页面 + API 同端口）
packages/web/
  renderer-v2/             前端源码
  vite.config.ts           构建配置（输出到 web_dist）
tests/                     SQLite 烟测
docker/                    Dockerfile、compose、入口脚本
var/                       上传文件、交付文件（运行时生成）
start.py                   本地启动脚本
```

## 快速开始

### 本地运行

```powershell
copy .env.example .env        # 填入 ZHIBIAO_AI_API_KEY
python start.py               # 自动检查依赖、启动本地 PostgreSQL、建表并创建管理员
```

访问 `http://127.0.0.1:8000/`，默认账号 `admin / sxit@123456`（可在 `.env` 中通过
`ZHIBIAO_BOOTSTRAP_USERNAME` / `ZHIBIAO_BOOTSTRAP_PASSWORD` 修改后首次启动生效）。

也可以手动启动：

```powershell
pip install -e .
cd packages\web; npm install; npm run build
uvicorn zhibiao.app:app --port 8000
```

### Docker

```powershell
copy docker\.env.docker.example docker\.env.docker   # 填入 ZHIBIAO_AI_API_KEY
docker compose -f docker/docker-compose.yml --env-file docker/.env.docker --profile infra up -d --build
```

访问 `http://127.0.0.1:8000/`。数据卷 `pgdata` 保存数据库，`appdata` 保存上传与交付文件。

一键脚本：`.\install.ps1`（Windows）或 `./install.sh`（Linux）。

## 配置

所有配置使用 `ZHIBIAO_` 前缀，完整清单见 `.env.example`。常用项：

| 变量 | 说明 |
|---|---|
| `ZHIBIAO_DB_TYPE` | `postgresql` / `mysql` / `sqlite` |
| `ZHIBIAO_DATABASE_URL` | PostgreSQL / SQLite 连接串 |
| `ZHIBIAO_AI_BASE_URL` / `ZHIBIAO_AI_API_KEY` / `ZHIBIAO_AI_MODEL` | OpenAI 兼容模型服务 |
| `ZHIBIAO_BOOTSTRAP_USERNAME` / `ZHIBIAO_BOOTSTRAP_PASSWORD` | 首次启动创建的管理员 |
| `ZHIBIAO_UPLOAD_ROOT` / `ZHIBIAO_DELIVERY_ROOT` | 上传与交付目录 |

模型服务也可以在登录后通过「模型服务」页面维护，数据库中的默认服务优先于环境变量。

## API

- 健康检查：`GET /api/health`
- 接口文档：`/docs`（OpenAPI）
- 认证：`POST /api/auth/login`、`GET /api/auth/me`，会话 Cookie `zb2_session`
- 业务：`/api/tenders`、`/api/tenders/{id}/analysis|outline|document|reviews|deliveries`
- 管理：`/api/admin/accounts|roles|providers|assistants`、`/api/templates`
- 统计：`GET /api/stats/overview`

## 测试

```bash
pytest -q          # SQLite 内存库烟测，不调用模型
```

## 许可

本项目以 **AGPL-3.0-only** 发布，完整条款见 [LICENSE](LICENSE)；第三方组件与网络服务义务说明见
[NOTICE.md](NOTICE.md)。依据 AGPL-3.0 §13，通过网络使用本程序的用户有权获取对应源代码。
