# 智标（ZhiBiao）

全流程智能招投标平台：从招标文件解析开始，完成**招标解读 → 大纲规划 → 正文编写 → 原文核对 → 修复 → 文档交付**。

页面与 API 由同一个 FastAPI 进程在同一端口提供，纯 B/S 部署，无 Nginx、无反向代理。可选开启 OCR 与标书生图能力，两项默认关闭。

> 以下截图使用演示项目「智慧校园综合管理平台建设项目」，数据均为示例。

## 界面

### 登录

登录页使用三维投标场景：中央是一本会翻开的投标文件，封面绕书脊打开、内页展开成扇形再合上，招标文件、AI 解读、标书章节、交付文件围绕它运转；鼠标移动会与节点产生交互；第二行标题刷新时逐字打出，并带闪烁光标。

![登录](docs/screenshots/login.jpg)

### 投标工作台

独立的三维投标流水线场景，把「招标文件 → 解读评分 → 标书章节 → 交付文件」串在一起；右侧为项目与资料指标。

![投标工作台](docs/screenshots/workbench.jpg)

### 投标项目

项目页集中展示项目身份、投标截止、工作阶段、资料数量与下一步动作。

![投标项目](docs/screenshots/project.jpg)

### 大纲与编写

左侧为章节目录，右侧随章节状态切换：正文尚未生成时展示「评分对齐」（评分项、分值、承载章节与目录覆盖率），正文生成后变为单章正文编辑器。章节的上移、下移、重新生成、保存与删除都放在编辑器上方，生成后检查挂在目录末尾「其他资料」下方；点击目录即可切换章节，未保存的修改会提示确认。招标解读、大纲规划、正文编写、质量检查四个步骤由 Agent 执行；支持标准结构、招标文件格式结构、目标篇幅、逐章编写、批量生成与失败重跑。

![大纲与编写](docs/screenshots/generation.jpg)

### 模型服务与能力开关

兼容 OpenAI 协议的模型服务配置；OCR 接口与标书生图在「能力开关」中配置，默认关闭，开启并保存后才会调用对应接口。

![模型服务](docs/screenshots/model.jpg)

## 使用流程

1. **登录**：账号登录后默认进入工作台。
2. **新建项目**：填写项目名称、采购人、预算与投标截止时间。
3. **上传资料**：支持 PDF、DOCX、TXT；扫描图片与扫描版 PDF 可在「模型服务 → 能力开关」开启 OCR 后识别。
4. **招标解读**：招标解析 Agent 读取原文，抽取项目事实台账，并生成项目概况、评分办法、实质性要求与风险项。
5. **大纲与编写**：生成规划 Agent 选择结构并生成大纲，正文编制 Agent 逐章写作；左侧章节目录选择章节，右侧单章编辑器直接修改正文，支持排序、单章重写与批量重跑。
6. **质量检查**：核对 Agent 按招标事实逐项核对成稿，修复 Agent 生成可应用的修订建议。
7. **文档交付**：导出 DOCX / PDF，交付历史可随时下载。

### 模型与能力配置

在右上角用户菜单进入「模型服务」：

- **服务列表**：填写兼容 OpenAI 协议的 `API Base`、`API Key`、模型与温度，可测试连通性并设为默认。
- **协作助手**：为招标解析、规划、编写、检查、修复分别指定模型或继承默认服务。
- **OCR 接口**：默认关闭。开启后，图片上传时自动识别文字；扫描版 PDF 由招标解析 Agent 按需识别并回写文本。可配置接口地址、Key、视觉模型与最多识别页数。
- **标书生图**：默认关闭。开启后只为示意图、流程图、架构图类占位框生成插图；证照、合同、截图等扫描件仍保留占位。可配置接口地址、Key、模型、图片尺寸、每章/每本上限，生成的图片随 DOCX 导出嵌入。

## 核心能力

| 模块 | 能力 |
|---|---|
| 招标解析 | PDF / DOCX / TXT 解析，抽取项目身份、8 项关键维度、评分办法、实质性要求与风险项 |
| Agent 事实台账 | 从原文抽取项目名称、编号、采购人、预算/限价、截止、有效期、工期、保证金、材料清单、技术参数、资格条件与承诺 |
| 三种结构 | 通用结构、按评分项对齐、按招标文件格式章生成；DOCX/PDF 模板可抽取章节骨架 |
| 正文生成 | 按章节 SSE 流式写作、批量生成、失败重跑、目标字数控制、截断续写、跨章去重、口径一致性检查 |
| 原文核对 | 项目身份、投标人名称、报价、工期、有效期、保证金、材料、参数、资格、承诺、模板覆盖、完整性 |
| 修复 | 按核对问题定位章节并改写，修改后重新核对 |
| OCR 接口 | 图片上传自动识别；扫描版 PDF 由招标解析 Agent 按需识别并回写文本 |
| 标书生图 | 为示意图类占位框生成插图，随 DOCX 导出嵌入；证照、合同、截图保留占位 |
| 能力开关 | 模型服务页统一配置 OCR 与标书生图，默认关闭 |
| 文档交付 | DOCX / PDF 生成，模板版式、目录页码、页眉页脚与宋体字体归一，交付历史可下载 |
| 平台管理 | 账号与角色、模型服务、协作助手、能力开关、文档模板、审计日志、统计 |

## 项目逻辑

### 主流程

```text
招标文件
  ├─ 解析文本（PDF / DOCX / TXT；扫描件走 OCR）
  ├─ 招标解读：维度、评分办法、实质性要求、风险
  ├─ 招标解析 Agent：形成事实台账（项目身份 / 材料 / 参数 / 资格 / 承诺 / 模板）
  ├─ 生成规划 Agent：选择通用 / DOCX 模板 / PDF 模板，生成大纲与目标字数
  ├─ 正文编制 Agent：逐章生成、续写与生成后检查
  ├─ 核对 Agent：用事实台账逐项核对成稿
  ├─ 修复 Agent：按问题改写并复查
  └─ 文档交付：DOCX / PDF
```

### 六个 Agent

| Agent | 职责 | 主要工具 |
|---|---|---|
| 招标解析 Agent | 抽取项目身份、材料、参数、资格、承诺、模板章节，保存事实台账并生成解读报告 | 压缩要点、OCR 识别、关键词检索、读解读、保存事实、生成解读 |
| 生成规划 Agent | 判断生成模式，建立大纲，分配目标字数与评分项 | 读事实、检测模板模式、抽取模板章节、建大纲、列章节 |
| 正文编制 Agent | 按大纲逐章写作，处理未完成/失败章节并做全书后处理 | 读事实、建大纲、列章节、写全书、成稿核对 |
| 核对 Agent | 按原文事实核对成稿，输出阻断/严重/主要问题与人工核对项 | 读事实、列章节、核对成稿、保存检查 |
| 修复 Agent | 按问题定位章节、改写并生成可应用的修订建议 | 读事实、列章节、读章节、生成修订 |
| 模板 Agent | 识别 DOCX/PDF 模板，抽取模板章节，核对覆盖情况 | 检测模式、抽取模板、列章节 |

Agent 采用工具循环：模型每轮输出一个 JSON 动作，服务端执行工具并继续，直到完成或达到最大步数。招标解读、大纲规划、正文编写、质量检查四个流程步骤默认由 Agent 执行；模型不可用时自动回落到同一份确定性实现，保证流程始终有结果。执行轨迹保存在任务记录中。

### 三种生成结构

1. **通用结构**：内置商务、资格、技术、服务、报价五类章节，适合多数项目。
2. **评分对齐**：由模型按评分项与实质性要求生成章节，评分项自动匹配并加权篇幅。
3. **按招标文件格式**：从招标文件的「投标文件格式」章节抽取章节骨架；DOCX/PDF 模板可抽取模板章节用于覆盖核对。

> 交付支持模板版式、目录与页码回填、页眉页脚、章节分页、Markdown 表格/图片嵌入；Word 与 PDF 统一宋体/思源宋体字形，PDF 由容器内 LibreOffice 转换。

### 核对范围

核对不再使用固定 21 项，而是基于招标原文事实逐项比对：

- 项目名称、项目编号是否贯穿成稿
- 投标人名称是否与营业执照等主体信息一致
- 报价是否超过最高限价/预算
- 总工期、投标有效期、投标保证金是否一致
- 招标文件要求的材料、证书、保证金回单等是否齐全
- 技术参数、数量、单位是否逐条响应
- 资格条件、实质性承诺是否体现
- 模板章节是否覆盖
- 章节是否为空或明显低于目标字数
- 签字、盖章、封装、正副本、开标原件等转为人工核对清单

### 生成后质量处理

- 跨章节重复长段落自动删除
- 工期、质保期、有效期、报价等口径冲突检测
- “详见招标文件 / 我方无法 / 仅供参考”等推诿表述拦截
- 空章节与字数不足检查

## 技术架构

- **后端**：Python 3.12+ / FastAPI / SQLAlchemy 2.0 异步 ORM / Pydantic v2
- **数据库**：PostgreSQL 16（默认）、MySQL 8、SQLite
- **模型**：任意 OpenAI 兼容接口；429 自动退避重试
- **扩展能力**：OCR 视觉接口、标书生图接口，均在模型服务页配置且默认关闭
- **前端**：React 19 / TypeScript 5 / Vite 7 / React Router 7 / Three.js
- **文档**：python-docx、pdfplumber、reportlab、LibreOffice headless（PDF 转换）

## 目录结构

```text
zhibiao/                  独立后端
  api/                    接口：认证、项目、文件、解读、大纲、编写、生成、Agent、检查、交付、账号、模板、统计
  agents/                 Agent 运行时、六个 Agent、事实台账、核对规则
  generation/             大纲规划、正文写作、生成任务、质量处理
  capabilities.py         OCR / 标书生图能力开关
  ocr.py                  OCR 接口（图片 / 扫描版 PDF）
  images.py               标书生图、图片存储与导出取图
  models.py               数据模型（21 张 zb_ 表）
  prompts.py              解读 / 大纲 / 写作 / 修订提示词
  app.py                  应用入口（页面 + API 同端口）
packages/web/
  renderer-v2/            前端源码
  vite.config.ts          构建配置
tests/                    后端测试
docs/screenshots/         README 截图
docker/                   Dockerfile、compose、入口脚本
scripts/                  安装、启动、停止脚本（install / start / stop）
var/                      运行时上传、交付与生图文件
```

## 部署

### 环境要求

- Python 3.12+
- Node.js 20+
- PostgreSQL 16（也可用 SQLite 快速体验）
- LibreOffice（导出 PDF 时需要；Docker 镜像已内置）

安装、启动与停止脚本统一放在 `scripts/`：
Windows 使用 `.\scripts\install.ps1`、`.\scripts\stop.ps1`，Linux 使用 `./scripts/install.sh`、`./scripts/stop.sh`。

### 方式一：一键安装（推荐）

Windows：

```powershell
.\scripts\install.ps1
```

Linux：

```bash
chmod +x scripts/*.sh
./scripts/install.sh
```

脚本会自动判断环境：Docker 可用时构建并启动容器，否则创建 `venv`、安装后端依赖、构建前端并本地启动。常用参数：

```powershell
.\scripts\install.ps1 -Mode local                         # 强制本地开发
.\scripts\install.ps1 -Mode docker                        # 强制 Docker
.\scripts\install.ps1 -AiApiKey sk-xxx -Yes               # 非交互并写入模型 Key
.\scripts\install.ps1 -SkipFrontend -NoStart              # 只准备环境
```

```bash
./scripts/install.sh --mode docker --ai-api-key sk-xxx --yes
./scripts/install.sh --mode local --skip-frontend --no-start
```

停止服务：

```powershell
.\scripts\stop.ps1            # 本地 API
.\scripts\stop.ps1 -Docker    # Docker 容器
```

```bash
./scripts/stop.sh             # 本地 API
./scripts/stop.sh --docker    # Docker 容器
```

### 方式二：本地手动运行

```powershell
copy .env.example .env
# 编辑 .env，至少填写 ZHIBIAO_AI_API_KEY

pip install -e .
cd packages\web
npm install
npm run build
cd ..\..

python scripts/start.py
```

`scripts/start.py` 会检查依赖、尝试启动本地 PostgreSQL、建表、创建初始管理员，然后启动服务。

也可手动启动：

```powershell
uvicorn zhibiao.app:app --host 0.0.0.0 --port 8000
```

访问 `http://127.0.0.1:8000/`，默认账号：

```text
admin / sxit@123456
```

首次启动后请在 `.env` 修改 `ZHIBIAO_BOOTSTRAP_USERNAME`、`ZHIBIAO_BOOTSTRAP_PASSWORD`，或登录后在账号权限页维护。

### 方式三：Docker

```powershell
copy docker\.env.docker.example docker\.env.docker
# 编辑 docker\.env.docker，填写 ZHIBIAO_AI_API_KEY

docker compose -f docker/docker-compose.yml --env-file docker/.env.docker --profile infra up -d --build
```

访问 `http://127.0.0.1:8000/`。compose 项目名固定为 `zhibiao`，数据卷：

- `zhibiao_pgdata`：PostgreSQL 数据
- `zhibiao_appdata`：上传文件、交付文件与生图文件

常用命令：

```powershell
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f api
docker compose -f docker/docker-compose.yml down
```

### 常用环境变量

| 变量 | 说明 |
|---|---|
| `ZHIBIAO_DB_TYPE` | `postgresql` / `mysql` / `sqlite` |
| `ZHIBIAO_DATABASE_URL` | 数据库连接串 |
| `ZHIBIAO_AI_BASE_URL` / `ZHIBIAO_AI_API_KEY` / `ZHIBIAO_AI_MODEL` | OpenAI 兼容模型服务 |
| `ZHIBIAO_BOOTSTRAP_USERNAME` / `ZHIBIAO_BOOTSTRAP_PASSWORD` | 初始管理员 |
| `ZHIBIAO_UPLOAD_ROOT` / `ZHIBIAO_DELIVERY_ROOT` / `ZHIBIAO_IMAGE_ROOT` | 上传、交付与生图目录 |
| `ZHIBIAO_ALLOWED_ORIGINS` | 额外跨域来源，逗号分隔 |

完整清单见 `.env.example`。

## API

- 健康检查：`GET /api/health`
- 接口文档：`/docs`
- 认证：`POST /api/auth/login`、`GET /api/auth/me`
- 项目：`/api/tenders`
- 解读：`POST /api/tenders/{id}/analysis`
- 大纲：`POST /api/tenders/{id}/outline`（`mode=score|standard|tender`，`targetWords`）
- 章节：`/api/tenders/{id}/sections`（新增、编辑、删除、上移下移、单章流式生成）
- 批量生成：`POST /api/tenders/{id}/generation`（`mode=missing|retry|all`）；状态 `/api/tenders/{id}/generation`、任务 `/api/generation/{job_id}`、取消 `/api/generation/{job_id}/cancel`
- Agent：`GET /api/agents`、`GET /api/tenders/{id}/agents/facts`
- 核对与检查：`POST /api/tenders/{id}/generation/postprocess`、`/api/tenders/{id}/reviews`、`/api/reviews/{review_id}`
- 交付：`POST /api/tenders/{id}/deliveries`、`GET /api/deliveries/{id}/download`
- 能力开关：`GET` / `PUT /api/admin/capabilities`（OCR 与标书生图，默认关闭）
- 图片：`GET /api/images/{image_id}`
- 管理：`/api/admin/accounts|roles|permissions|audit|providers|assistants|capabilities`、`/api/templates`
- 统计：`GET /api/stats/overview`

## 测试

```bash
pytest -q
```

测试使用 SQLite 临时库，不调用真实模型，覆盖认证、项目、文件、标准大纲、章节编辑、批量生成任务、Agent 工具循环、事实规范化、核对规则、能力开关默认关闭与生图占位过滤。

前端构建：

```bash
cd packages/web
npm run build
```

## 许可

本项目以 **AGPL-3.0-only** 发布，完整条款见 [LICENSE](LICENSE)；第三方组件与网络服务义务说明见 [NOTICE.md](NOTICE.md)。
依据 AGPL-3.0 §13，通过网络使用本程序的用户有权获取对应源代码。
