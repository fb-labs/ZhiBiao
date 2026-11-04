# 来源、许可与第三方组件说明（NOTICE）

本文件依据 **AGPL-3.0 第 5 条**（保留版权声明、标注修改）编写，供使用者与下游分发者查阅。

## 1. 本仓库

| 项目 | 内容 |
|---|---|
| 仓库 | https://github.com/fb-labs/ZhiBiao |
| 维护者 | Foulereee <1817558146@qq.com> |
| 当前许可 | **GNU Affero General Public License v3.0（AGPL-3.0-only）**，完整条款见 [LICENSE](LICENSE) |
| 版权 | `Copyright (c) 2026 Foulereee` |

## 2. 实现说明

本仓库后端为独立实现的 `zhibiao/` 包：21 张 `zb_` 前缀数据表、`/api` 接口、进程内会话认证、
独立编写的业务提示词与规则。前端为 `packages/web/renderer-v2`，构建产物由 FastAPI 同端口托管。
当前代码树中的后端与前端均由本仓库独立实现，整体按 AGPL-3.0-only 发布。

## 3. AGPL-3.0 第 13 条（网络服务）义务

本程序以 **B/S（浏览器访问）** 形态通过网络对外提供服务。依据 AGPL-3.0 第 13 条，
**任何通过网络与本程序交互的用户，都有权获取其对应的完整源代码**。

部署者应当至少满足其中一项：

1. 在服务的“关于/帮助”页面或等价位置提供指向本仓库的源码链接；或
2. 提供书面要约，承诺向任何第三方提供对应源代码。

若你修改本程序并对外提供服务，你同样需要按 AGPL-3.0 §5、§13 公开你的修改。

## 4. 第三方组件

> 本节列出当前代码树的主要运行时依赖。版本号以 `pyproject.toml` / `package.json` 为准，
> 各组件分别遵循其原始许可。

**后端主要依赖**

| 组件 | 许可 |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| SQLAlchemy | MIT |
| asyncpg | Apache-2.0 |
| aiomysql | MIT |
| aiosqlite | MIT |
| Pydantic / pydantic-settings | MIT |
| OpenAI Python SDK | Apache-2.0 |
| bcrypt | Apache-2.0 |
| python-docx | MIT |
| pdfplumber | MIT |
| reportlab | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| httpx | BSD-3-Clause |

**前端主要依赖**：React 19 / React DOM 19 / React Router 7 / Vite 7 / TypeScript 5 /
Three.js，均为 **MIT** 或 **Apache-2.0** 许可。

**特别提示**

1. PDF 交付使用 reportlab，并在运行时加载系统/容器内的中文字体（不随仓库分发字体文件）。
2. 完整条款以各组件包内 `LICENSE` 文件为准。
