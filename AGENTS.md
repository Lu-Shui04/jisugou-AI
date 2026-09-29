# AGENTS.md — 给 AI 助手的工作约定

> 这份文件放在仓库根目录，**每次改动都要遵守**。人类协作者看 [README.md](README.md) 了解项目本身。

## 一、最重要的约定：改完就存档（用户明确要求）

**每次改完代码，必须立刻 git 存档并推送**，保证任何一步都能回退：

```bash
git config user.email "281350384+Lu-Shui04@users.noreply.github.com"   # 已设好，一般不用重复
git add -A
git commit -m "说明这次改了什么"
git push
```

- 远端：`origin = https://github.com/Lu-Shui04/jisugou-AI.git`，分支 `py`（已设置跟踪，直接 `git push`）
- 旧仓库地址保留在 `old-origin`，别再往那推
- **一次改动 = 一次提交**：不要攒着一起提交，也不要把无关改动混进同一个提交
- 提交信息用中文，一句话说清"改了什么、为什么"（例：`fix: 基础对话不再自动补人工电话`）
- 只提交代码与文档，**不要**把下面"禁止提交"里的东西加进来

## 二、提交安全红线（这条踩了很难收拾）

| 绝对不能提交 | 原因 |
| --- | --- |
| `server-py/.env` | 里面有真实 API Key 与数据库密码（已被 .gitignore 忽略，别手滑 `git add -f`） |
| `_kb_bench/` | 约 200MB 的本地实验脚本与中间结果（目录已移出仓库，放在 `D:\tmp-deploy\_kb_bench`，别搬回来） |
| `client/dist/`、`client/dist-jisu/` | 构建产物，重新构建即可 |
| `server-py/_wsl_admin.sh` | 个人本机脚本，含本机绝对路径 |

提交前先自查：`git add -A --dry-run | Select-String '.env|_kb_bench|dist|_wsl_admin'`，输出为空才安全。

## 三、改完代码要跑的验证

```bash
# 单元测试（离线，不需起服务；约 12 秒，57 条黄金测试）
cd server-py && python -m unittest discover -s tests -t .

# 线上业务验收（4 个入口 + 退款三态 + 注入拦截 + 后台，16 项，约 20 秒）
python D:\tmp-deploy\live_business_test.py http://139.199.4.231:8050
```

改到"用户看得见的行为"（页面、话术、接口返回）时，**必须**跑一次线上验收，别只看单测。

## 四、部署方式（改完代码还要让线上生效）

| 环境 | 怎么起 | 地址 |
| --- | --- | --- |
| 本地前端 | `cd client && set VITE_DEV_API=http://localhost:3001 && npm run dev` | http://localhost:5174 |
| 本地后端 | WSL 里 `cd .../server-py && APP_PORT=3001 docker compose up -d --build` | http://localhost:3001 |
| 线上 | 用 `D:\tmp-deploy\` 下的部署脚本（不在仓库里，含服务器凭据） | http://139.199.4.231:8050 |

⚠️ **代码是打进 Docker 镜像的**：改完必须 `docker compose up -d --build`，只 `restart` 不生效（本地和线上都一样）。前端是 hash 命名资源，用户浏览器可能还跑着旧 JS —— 页面右下角会自动提示"已更新到新版本，点这里刷新"。

## 五、代码结构速查

| 位置 | 说明 |
| --- | --- |
| `server-py/app/routers/` | 四个入口的 SSE 接口：chat / agent / rag / graph |
| `server-py/app/graphs/` | LangGraph 工作流（意图识别 → 条件边 → 订单/知识库/闲聊 → 汇总） |
| `server-py/app/prompts/` | **提示词层**：全站提示词的唯一定义处，节点里不留文案 |
| `server-py/app/models/` | **模型层**：对话模型（deepseek.py）/ Embedding 怎么建、怎么调 |
| `server-py/app/retrieval/` | **检索层**：rag_chain（阈值过滤 + 生成）/ rerank（精排）/ query_utils（归一、关键词兜底、RRF）/ kb_bridge（借用链路） |
| `server-py/app/security/` | 四层提示词防护（白名单 / 规则 / 小模型 / 输出检查）+ 身份令牌 + 滑块门禁 |
| `server-py/app/resilience/` | **韧性层**：circuit（熔断三态机）/ model_fallback（模型降级）/ tool_guard（工具超时重试） |
| `server-py/app/observability/` | **观测层**：usage（Token 用量 + 埋点桥）/ chatlog（对话记录，Redis）/ trace + trace_store（**全链路追踪**：run + step 落 PostgreSQL） |
| `server-py/app/db/` | PostgreSQL 连接串 + asyncpg 连接池 / Redis / 会话解析（历史数据权限裁剪） |
| `server-py/app/utils/handoff.py` | 退款族意图判定与"人工接力"话术 |
| `server-py/app/utils/grounding.py` | 答案接地校验（防编造订单） |
| `server-py/tests/` | 57 条单测（security / order / resilience / rag / handoff / prompts / trace 七个文件）+ 3 套评测集（注入 17 / 检索 7 / 退款意图 18 = 42 条） |
| `client/src/` | Vue3 前端（四个业务页 + 管理后台 9 个页签） |

## 六、风格要求（用户偏好）

- 回复用中文，**给实测证据**（真实接口返回、真实数字），不要只说"已完成"
- 有线上真实事故时，写清"现象 → 根因 → 修法 → 验证"，这类内容用户会拿去面试讲
- 改动尽量小而聚焦；改完顺手把单测/评测集补上，测试与评测是这套项目的卖点之一
- **测试总数有硬上限**：单测 + 评测用例合计 **≤ 100 条**（当前 57 + 42 = 99）。这是用户明确要求：
  宁可少而准，也不要一堆乱七八糟的。要加新用例，就先把被它取代的旧用例合并或删掉，别让总数涨上去
- 只留"黄金测试"：每条都要能说清它拦的是哪类错（线上事故回归 / 三态与边界 / 拦截与放行的对照）。
  说不清、或换个实现照样通过的，直接删
