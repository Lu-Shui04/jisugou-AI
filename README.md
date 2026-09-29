# 极速购 AI 客服系统 — Python 版运行手册

> 想快速找文件？先看第 0 章「代码结构」，按层找。

## 0. 代码结构：按层找文件

```
server-py/app/
├── main.py              FastAPI 入口：挂路由、装身份中间件
├── prompts/             提示词层：全站提示词的唯一定义处（节点里不留文案）
├── models/              模型层：对话模型 / Embedding 怎么建、怎么调
│   ├── deepseek.py         create_model()：超时 + 重试 + 备用模型降级
│   └── embedding.py        带熔断保护的 Embedding 包装
├── retrieval/           检索层：向量召回 → RRF 融合 → 重排 → 关键词兜底
│   ├── rag_chain.py        相似度阈值过滤 + 生成，并返回来源
│   ├── rerank.py           硅基流动 bge-reranker 精排（可关，默认关）
│   ├── query_utils.py      查询归一 / 关键词兜底 / RRF / 引用编号处理
│   └── kb_bridge.py        给"没有知识库"的入口借用同一条检索链路
├── graphs/              多节点工作流：意图识别 → 条件边 → 订单/知识库/闲聊 → 答案合成
├── agents/              工具 Agent（订单查询页）
├── chains/              纯 LCEL 链（基础对话页）
├── security/            安全层：白名单→规则→小模型→输出检查 + 身份令牌 + 滑块门禁
├── resilience/          韧性层：熔断三态机 + 模型降级 + 工具超时重试
├── tools/               工具层：订单 / 物流 / 用户订单查询（每次调用都校验数据归属）
├── observability/       Token 用量与对话记录（存 Redis，不可用时降级内存）
├── db/                  Postgres / Redis / 会话解析（含历史数据权限裁剪）
├── routers/             HTTP 接口层（很薄：只做协议解析 + SSE 推送）
├── utils/               接地校验、人工接力判定、ID 解析、消息工具
├── data/                演示数据（mock.py）与知识库原文（knowledge/*.md）
└── scripts/             ingest：知识库切分入库
```

三条找文件的捷径：

| 想改什么 | 去哪 |
| --- | --- |
| 话术 / 提示词 / 规则句 | `app/prompts/`（改完跑 `tests/test_prompts.py`） |
| 模型参数、超时、降级、熔断 | `app/models/` + `app/resilience/` |
| 召回、相似度阈值、重排 | `app/retrieval/` |

## 1. 环境准备

```bash
cd server-py
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 配置环境变量

复制示例文件并填入真实配置：

```bash
cp .env.example .env
```

`.env` 需要填写：

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek 对话模型 Key（必填） |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com/v1` |
| `MODEL_NAME` | 默认 `deepseek-chat` |
| `ZHIPU_API_KEY` | 智谱 AI Embedding Key（RAG 功能必填，二选一） |
| `DASHSCOPE_API_KEY` | 阿里云百炼 Embedding Key（二选一，需同时修改 `app/models/embedding.py` 启用对应代码块） |
| `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_DATABASE` | PostgreSQL 连接信息（RAG 功能必填） |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 管理员后台账号密码，默认 `admin` / `123456` |
| `ADMIN_TOKEN_TTL` | 登录令牌有效期（秒），默认 8 小时 |
| `USAGE_RETENTION_SECONDS` / `CHATLOG_RETENTION_SECONDS` | 用量统计 / 对话记录保留时间（秒），默认 7 天 |

## 3. 准备 PostgreSQL + pgvector

确保本地/远端 Postgres 已安装 `pgvector` 扩展，并创建好 `.env` 中指定的数据库：

```sql
CREATE DATABASE jisu_ai;
\c jisu_ai
CREATE EXTENSION IF NOT EXISTS vector;
```

> 知识库表 `knowledge_embeddings` 由 `langchain_postgres.PGVector` 自动创建，无需手动建表。

## 4. 知识库入库（RAG 功能必做，且每次更新知识库后需重新执行）

```bash
python -m app.scripts.ingest
```

成功后会输出「切分完成」「入库完成」。该脚本会清空并重建 `knowledge_embeddings` 表中的数据。

## 5. 启动服务

```bash
uvicorn app.main:app --reload --port 3000
```

启动后访问 `http://localhost:3000/` 应返回服务信息 JSON。

前端（Vue3 + Vite）：

```bash
cd client
npm install
npm run dev          # http://localhost:5173
```

## 6. 接口列表

| 接口 | 说明 |
| --- | --- |
| `GET  /api/chat/health` | 健康检查 |
| `POST /api/chat` | 普通对话（一次性返回） |
| `POST /api/chat/stream` | 流式对话（SSE） |
| `POST /api/agent/stream` | 客服 Agent 对话（自动调用订单/物流工具，SSE） |
| `POST /api/rag/query` | 知识库问答（SSE） |
| `POST /api/graph/stream` | LangGraph 多节点工作流对话（意图路由 → 订单/知识库/闲聊 → 答案合成，SSE） |
| `GET  /api/observability/usage` | Token 用量汇总（公开只读） |

四个对话接口都接受可选的 `user_id` / `user_name` 字段，前端会自动带上访客标识，管理员后台据此区分「不同用户与 AI 的聊天记录」。

### 请求示例

```bash
# 普通对话
curl -X POST http://localhost:3000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'

# Agent 对话（订单查询）
curl -N -X POST http://localhost:3000/api/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我查一下订单 ORD-001 的状态"}'

# RAG 知识库问答
curl -N -X POST http://localhost:3000/api/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question": "退换货政策是什么？"}'

# Graph 工作流对话
curl -N -X POST http://localhost:3000/api/graph/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "我的快递到哪了，单号 SF1234567890"}'
```

SSE 接口需加 `-N` 参数禁用 curl 缓冲，才能看到流式输出。

## 7. 管理员后台

前端**左上角**「管理员入口」按钮进入 `/admin`（默认账号 `admin`，密码 `123456`，可用 `.env` 的
`ADMIN_USERNAME` / `ADMIN_PASSWORD` 修改）。登录成功后拿到访问令牌，后续请求都带
`X-Admin-Token` 头；连续输错 5 次会锁定 5 分钟。

后台能力：

| 页面 | 内容 |
| --- | --- |
| 总览看板 | 今日/累计请求数、Token 消耗、活跃用户、平均耗时、错误率；近 N 天 Token 趋势图、入口与模型分布、最近对话 |
| Token 统计 | 按入口（chat / agent / rag / graph）、按模型、按用户、按天的请求数、输入/输出/总 Token、平均耗时、错误率，支持切换日期与趋势天数 |
| 对话记录 | 不同用户的每轮问答：用户、会话、入口、问题、回答、Token、耗时、状态；支持按用户 / 入口 / 状态 / 日期 / 关键词筛选与分页，可导出 CSV |
| 检索与重排 | 知识库检索明细：Top-K 候选片段、相似度得分、阈值、保留/被过滤、兜底降级原因 |
| 用户列表 | 每位访客的对话轮数、累计 Token、错误数、首次与最近活跃时间、各入口使用分布 |
| 会话缓存 | 按 `session_id` 查看 Redis 中的会话上下文与剩余 TTL，可一键清空 |
| 系统状态 | Redis / PostgreSQL 连接状态、知识库片段与来源数、模型与 RAG 配置、各类数据保留策略 |

对应的后端接口（`/api/admin`，除 `login` 外都需要 `X-Admin-Token`）：

| 接口 | 说明 |
| --- | --- |
| `POST /api/admin/login` | 账号密码登录，返回令牌 |
| `POST /api/admin/logout` | 退出登录 |
| `GET  /api/admin/me` | 当前登录信息 |
| `GET  /api/admin/overview` | 看板总览（Token / 请求 / 用户 / 趋势 / 最近对话） |
| `GET  /api/admin/usage` | Token 用量明细（按入口 / 模型 / 用户 / 日期） |
| `GET  /api/admin/users` | 用户列表（对话轮数 / Token / 最近活跃） |
| `GET  /api/admin/conversations` | 聊天记录分页查询（user_id / session_id / route / keyword / day / status） |
| `GET  /api/admin/conversations/{trace_id}` | 单轮详情（含检索与重排明细、工具调用轨迹） |
| `DELETE /api/admin/conversations/{trace_id}` | 删除单轮记录 |
| `GET  /api/admin/retrievals` | 最近的知识库检索 + 重排明细 |
| `GET  /api/admin/sessions/{session_id}` | 会话缓存内容与剩余 TTL |
| `DELETE /api/admin/sessions/{session_id}` | 清空会话缓存 |
| `GET  /api/admin/system` | 系统状态与配置 |
| `GET  /api/admin/export/conversations.csv` | 导出聊天记录 CSV（带 BOM，Excel 可直接打开） |

审计数据存在 Redis（默认保留 7 天，可用 `CHATLOG_RETENTION_SECONDS` 调整）：

- `chatlog:turn:{trace_id}`：单轮记录（问题、回答、Token、耗时、工具步骤、检索与重排明细）
- `chatlog:all` / `chatlog:index:{日期}`：全局与按日时间线
- `chatlog:user:{user_id}` / `chatlog:userstat:{user_id}`：用户时间线与用户汇总
- `usage:total` / `usage:{日期}` / `usage:route:*` / `usage:model:*` / `usage:user:*`：Token 用量多维汇总

> Redis 不可用时，用量与对话记录会降级写入进程内存（仅保留最近若干条），不影响对话主流程。

## 8. 常见问题

- **启动时报 Postgres 连接/密码错误**：`app/retrieval/rag_chain.py` 在模块导入时会立即连接数据库初始化向量库，确保 `.env` 中的 Postgres 配置正确且服务已启动，再启动 FastAPI。
- **RAG 查询无结果**：检查是否已执行第 4 步的 `ingest` 脚本。
- **模型调用报 401/403**：检查 `DEEPSEEK_API_KEY` / `ZHIPU_API_KEY` 是否正确、额度是否充足。
- **后台显示「登录已过期」**：令牌默认 8 小时过期，重新登录即可；也可调大 `ADMIN_TOKEN_TTL`。
- **对话记录里出现「匿名」用户**：该轮请求没带 `user_id`（例如直接用 curl 调用），前端页面会自动带上访客标识。
