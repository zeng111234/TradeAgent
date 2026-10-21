# TradeAgent — AI 外贸智能获客助手

> 基于 Python + FastAPI + Vue3 的外贸客户开发与邮件营销工具。
> **客户筛选与跟进判断由 agent 用工具完成，不再由硬编码公式决定。**

## 项目背景

针对外贸业务员在使用富通天下等平台时的痛点，打造一个更智能、更低成本的获客与客户管理工具。

## 为什么这是一个 agent

最初的版本叫 TradeAgent，但 LLM 从未做过任何决定。所有判断都写在 Python 里，模型只在最后被调用一次，用来生成文案。三处证据：

**1. 没有工具调用能力。** 每次模型调用都是：

```python
response = await client.chat.completions.create(
    model=..., messages=[...], temperature=..., max_tokens=...,
)
content = response.choices[0].message.content    # 只读文本，结束
```

没有 `tools` 参数，也不解析 `tool_calls`。模型能说话，不能动手。

**2. 判断是一个公式。** `scripts/daily_scan.py` 用四个魔法数字决定哪些客户存在：

```python
score = 20
if is_b2b:              score += 30
if buyer_keyword:       score += 20
if product_keyword:     score += 20
if emails_found:        score += 10
if score >= 40:         keep()
```

`auto_lead_scanner` 有更大的一套，而且更糟 —— 它在**调用模型之前**就执行了，模型只能对已经通过公式筛选的名单排序，而那个数字被当成 `/100` 的评估分展示。

**3. 控制流是 Python。** `workflows.py` 开头写着 *"the 'brain' that connects all tools"*，实际是固定的 `await` 序列；`scheduler.py` 写着 *"the 'brain'"*，实际是定时器。

现在：agent 拿到目标，自己决定搜什么、打开哪个候选、拒绝谁以及为什么，并记录自己的判断和依据。Python 只保留真正确定性的部分 —— 已抓取域名的记忆、数量上限、写文件。

```
同一个 loop，同一套工具，两个不同目标：
  找新客户    search_prospects -> inspect_website -> classify_prospect
  查老客户    list_customers -> get_customer_signals -> get_communication_history
```

循环里没有任何代码知道哪个目标该用哪些工具。详细设计见 [`docs/agent-design.md`](docs/agent-design.md)。

## 核心功能

### 1. 智能客户开发（获客）
- agent 自主搜索潜在买家，逐站阅读并判断，记录打分的理由与证据
- 从公开数据源（海关数据、行业目录）获取潜在买家信息
- 自动生成个性化英文开发信（**仅生成草稿**）

### 2. 邮件营销自动化
- 邮件模板管理（支持变量替换：公司名、联系人、产品等）
- 批量发送 + 定时发送
- 邮件打开/回复追踪
- AI 自动分类回复（询价/拒绝/待跟进/成交）

### 3. 客户 CRM 管理
- 客户标签分类（按行业/地区/采购意向/跟进阶段）
- 跟进提醒和待办事项
- 客户沟通时间线
- 报价单生成

### 4. 数据分析看板
- 获客转化漏斗
- 邮件打开率/回复率统计
- 客户来源分析
- 销售预测

### 5. Agent 运行轨迹
- 每一轮：模型说了什么、调用了哪些工具、参数是什么、返回了什么、耗时多少
- 轨迹渲染成 Mermaid 时序图 / 流程图 + 独立 HTML 页面
- 全部落库，可回放、可审计

### 6. 长任务不炸上下文
- 上下文超预算时自动压缩：系统提示与最近几轮永不裁剪，只裁旧的工具输出
- 裁掉的内容**写入磁盘并可读回**（`recall_observation`），不是丢掉
- 裁剪幅度按缺口缩放；实在压不下去时报告 `unreachable`，不假装成功

### 7. 子 agent 委派
- `spawn_researcher` 把「读一大堆材料」这件事推到独立上下文里
- 子 agent 拿到的工具表**去掉了写工具**，也去掉了委派工具，**深度上限 1**
- 返回的是结论 + 证据来源，不是它的完整过程；子 agent 失败不会中断父 run

## Agent 权限边界

**agent 可以调查、阅读、准备草稿。它不能发送。**

这个边界不是靠提示词实现的，而是靠**工具不存在**：

| 不存在的工具 | 原因 |
|---|---|
| 任何 `send_*` | 外发给真实客户必须由人触发 |
| 任何 `delete_*` | 破坏性操作不应由 agent 自主执行 |
| 改写人工维护的内容 | agent 静默改写别人的数据，比多一条重复记录更糟 |

- `create_email_draft` 只写入 `pending` 状态的草稿，由现有两步式人工审核流程消费
- `upsert_customer` 遇到已存在的客户返回 `skipped`，绝不覆盖
- 能碰已有记录的工具只有四个，且各自只动一个字段：`complete_task`（状态）、`reschedule_task`（截止日期）、`update_customer_stage`（阶段）、`reject_email_draft`（撤回待审草稿，记录保留不删除）
- `GET /api/v1/agent/capabilities` 会把这个边界显式列出来，不必读源码就能确认

`tests/test_agent_tools_policy.py` 断言：没有任何工具名匹配 `send` / `dispatch` / `delete` / `remove` / `drop` / `purge`，且 agent 包内不存在任何 SMTP 引用。

## 快速开始

### 后端启动
```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

### 前端启动
```bash
cd frontend
npm install
npm run dev
```

### Docker 一键启动
```bash
docker-compose up -d
```

### 运行 agent

```bash
cd backend
export OPENAI_API_KEY=...            # 必填，没有降级方案

python -m app.cli tools              # 看 agent 能用哪些工具
python -m app.cli run "看看我现有客户里哪些需要今天跟进，给我理由"
python -m app.cli run "找 5 家德国刺绣线买家" --html ./out
python -m app.cli runs --limit 10    # 历史运行
python -m app.cli show <run_id> --mermaid
```

**没有 API key 也能看它工作**（离线，不打网络，不调模型）：

```bash
python scripts/daily_scan.py --demo          # 完整每日扫描流程 + 报告
python scripts/demo_agent_run.py             # 两个不同目标 → 两条不同工具路径
```

### 测试

三层，各自需要的东西不同，所以分开跑：

```bash
cd backend
python -m pytest                    # 1015 个，约 60 秒，不需要网络

cd frontend
npm test                            # 41 个纯函数用例（node --test，零依赖）
npm run build

npm run test:browser                # 4 条真实浏览器流程（需要 playwright-core + 一个 Chromium）
```

`test:browser` 自己起 vite、拦截所有 `/api/v1` 请求，**不需要后端，不需要凭据**。
它覆盖的是三件曾经真实发生、单元测试看不见的事：完成任务失败后状态回滚、
SSE 连不上时降级为轮询、断流后用 `after=<最后一轮>` 续传（外加质量页不把"没有样本"画成 `0%`）。
没装依赖时：

```bash
cd frontend
npm install --no-save playwright-core@1.63.0
node node_modules/playwright-core/cli.js install chromium
```

### 运维视图

```bash
# 最近 14 天的运行/任务/审批趋势：成功率、P50/P95、stop_code、跳过的任务
curl 'localhost:8000/api/v1/analytics/quality?days=14'

# 熔断器的当前状态与计数：开过几次、拒过几次、放行过几个试探
curl 'localhost:8000/api/v1/diagnostics'

# 真上游的形状体检：文本 / usage / tool_call 三件事，漂移退出 2，连不上退出 3
python scripts/contract_canary.py

# 熔断状态机的故障注入：假时钟 + 假 provider，不烧 token
python scripts/circuit_probe.py
```

`/analytics/quality` 读的是已有的 `agent_runs` / `job_runs` / `email_logs`，**不读 trace**；
某天没有运行会显示为 0，而不是从序列里消失。它度量的是"系统有没有变差"，不是"答案好不好"——
后者这个仓库没有 rubric，也没有假装有。

`closed_loop` 那一段回答的是另一个问题：**系统建议发的那封邮件，有没有被回复**。
`email_logs.suggestion_kind` / `suggestion_attempt` 在发送时写入（迁移 0010），报表按
建议类型和"第几次跟进"分组；没有来源的邮件（人工撰写、agent 草稿、早于该字段的旧邮件）
单独记作 `unattributed` 并给出 `coverage`，模拟发送与退信计入 `excluded` 而不是分母。
前端入口是 `/quality`。

## 发件邮箱（可切换）

发件邮箱不再是写死在 `.env` 里的一个 `SMTP_USER`。侧边栏「系统 → 发件邮箱」页可以管理多个
邮箱，**同一时间只有一个生效**，之后发出的邮件都从当前生效的邮箱离开：

- 新增时勾选「创建后立即启用」即切换；日常切换点列表里的「启用」，即时生效、无需重启
- 编辑时授权码留空表示不变——换名字不需要把授权码翻出来重贴
- 密码只写不返：任何接口都不返回已保存的授权码，页面上也只在输入框出现一次
- 数据库层保证不出现两个生效邮箱（部分唯一索引），服务层的切换与它是双保险
- 删除当前生效的邮箱**不会**自动顶替另一个：之后的发送回退到 `.env` 配置，未配置则为模拟
  发送（邮件页会打 `simulated` 标签，不会冒充真实投递）
- Agent 没有操作这些记录的工具——用哪个邮箱发件是身份决策，不是工具调用能做的事

没有启用任何页面邮箱时，行为与以前完全一致：读取 `.env` 的 `SMTP_*`。
`GET /api/v1/senders/effective` 随时回答"现在发会从哪个邮箱走"，不真的发任何东西。

## 线上部署：一个链接，整栈可用

本项目**不适合 GitHub Pages**：那只能托管静态文件，而这里的数据库、LLM 调用、发信、
定时扫描、SSE 流式全都活在 FastAPI 进程里。前端 11 个页面首屏都请求 `/api/v1/**`，
全站没有示例数据，纯静态发布只会得到一个能导航、但每条列表都为空的壳。

现在的做法是**整栈发布在一个端口上**：FastAPI 同时提供 `/api/v1/**` 和前端页面。

```
npm run build:deploy          # 前端产物输出到 backend/webroot/（不是 dist/：发布会跳过
                              # 它认为是构建产物的目录，那样发出去就没有前端了）
cd backend && sh start.sh     # 绑定 $PORT、固定 SQLite 路径、启动 uvicorn
```

后端托管前端的规则（`backend/app/main.py`）：

- API 路由先注册，SPA 回退最后注册，所以 `/api/v1/**` 永远不会被前端抢走
- `/console`、`/customers/3` 这类前端路由冷启动和刷新都能直达应用
- API 形状但没人认领的路径仍然返回 JSON 404，不会拿 HTML 冒充接口
- 没有构建产物时（纯 API 安装、测试运行），`/` 照旧返回原来的问候 JSON；
  那份问候现在也有固定位置：`GET /api/v1/info`

### 访问密码（公开部署必开）

`backend/.env` 里设 `APP_PASSWORD` 即启用共享密码门（`backend/app/access.py`）：
`/login` 登录页 + HMAC 签名 cookie，12 小时有效；**留空则完全不生效**，本地开发行为不变。

- 未登录：页面请求 302 到 `/login?next=...`，API 请求返回 `401 {"code":"unauthenticated"}`
  （不是 HTML——否则调用方会把登录页当数据解析）
- `/health`、`/ready`、`/assets/**` 放行，否则平台探活会把它自己判死
- 4 位一类短密码在公网上几秒就能枚举，所以失败计数按来源和全局双份统计：
  单来源 5 次失败锁 15 分钟，全局短时间爆发则暂停登录
- `next` 只允许站内路径，`https://evil.example/` 一律收敛到 `/`（挡开放重定向）

### 已知边界

- 线上 SQLite 数据在**同一应用的重新部署后仍然保留**（实测：写入一条待办 → 重新发布 →
  仍在）。但沙箱不是数据库服务，仍建议线上只当演示，真实数据留本地库
- 拿到密码的人就能消耗你的 LLM 额度、并用你配置的邮箱发信。密码要换只改 `.env` 一行

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | agent 功能必填 | 没有它 agent 直接拒绝运行，不会伪造结果 |
| `OPENAI_BASE_URL` | 可选 | 默认 `https://api.xiaomimimo.com/v1`，兼容 DeepSeek / OpenAI |
| `OPENAI_MODEL` | 可选 | 默认 `mimo-v2.5` |
| `SMTP_*` / `REPORT_EMAIL` | 可选 | 每日报告邮件；不配置则打印到 stdout。客户邮件优先用「发件邮箱」页里生效的账户，未启用任何账户时才回退到这里的 `SMTP_USER` |
| `SCAN_KEYWORDS` / `SCAN_COUNTRIES` | 可选 | 每日扫描的关键词与市场 |
| `SCAN_DRY_RUN` | 可选 | `1` = 不写文件、不发邮件 |
| `AGENT_SPILL_DIR` | 可选 | 压缩时溢出的完整观察文本存放位置（默认系统临时目录） |
| `WEBSITE_TLS_VERIFY` | 可选 | 抓取客户网站时校验 TLS，默认 `true`；只在明确接受风险时设为 `false` |

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.11 + FastAPI |
| 数据库 | SQLite（开发） / PostgreSQL（生产） |
| ORM | SQLAlchemy |
| 前端 | Vue3 + Vite + Element Plus |
| AI | OpenAI 兼容 API（原生 function calling） |
| 爬虫 | requests + BeautifulSoup + ddgs |
| 部署 | Docker + docker-compose |

## 项目结构

```
TradeAgent/
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI 入口
│   │   ├── cli.py              # 命令行入口
│   │   ├── config.py           # 配置管理
│   │   ├── database.py         # 数据库连接
│   │   ├── agent/              # agent 运行时
│   │   │   ├── protocol.py     #   消息与工具调用协议
│   │   │   ├── loop.py         #   有界 ReAct 循环
│   │   │   ├── tools.py        #   工具注册表（schema 由签名推导）
│   │   │   ├── trade_tools.py  #   CRM / 获客工具
│   │   │   ├── scan_tools.py   #   每日扫描工具 + 落盘
│   │   │   ├── scan_runner.py  #   每日扫描编排
│   │   │   ├── service.py      #   配置 → 客户端 → 运行 → 落库
│   │   │   ├── repository.py   #   运行记录持久化
│   │   │   └── render.py       #   轨迹 → Mermaid / HTML
│   │   ├── models/             # 数据库模型
│   │   ├── routers/            # API 路由
│   │   ├── services/           # 业务逻辑
│   │   └── utils/              # 工具函数
│   ├── tests/                  # 232 个测试
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                   # Vue3 前端
├── scripts/
│   ├── daily_scan.py           # 每日扫描（agent 驱动）
│   └── demo_agent_run.py       # 离线演示
├── docs/agent-design.md        # agent 设计说明
└── README.md
```

## 尚未完成

- `agent_service.py` 中的流失预警与跟进建议函数仍带有硬编码阈值，尚未拆分
- `POST /api/v1/agent/run` 是同步的，会占用连接直到运行结束；生产环境应改为入队 + 轮询（存储层已具备）
- 前端尚未接入轨迹视图（HTML 轨迹页已可通过 `/api/v1/agent/runs/{id}/trace.html` 访问）

## 作者

GitHub: [@zeng111234](https://github.com/zeng111234)

## License

MIT
