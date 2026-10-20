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
| 修改已有客户/邮件记录 | agent 静默改写人工维护的数据，比多一条重复记录更糟 |

- `create_email_draft` 只写入 `pending` 状态的草稿，由现有两步式人工审核流程消费
- `upsert_customer` 遇到已存在的客户返回 `skipped`，绝不覆盖
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

```bash
cd backend
python -m pytest          # 996 个，约 57 秒，不需要网络
```

### 运维视图

```bash
# 最近 14 天的运行/任务/审批趋势：成功率、P50/P95、stop_reason、跳过的任务
curl 'localhost:8000/api/v1/analytics/quality?days=14'

# 真上游的形状体检：文本 / usage / tool_call 三件事，漂移退出 2，连不上退出 3
python scripts/contract_canary.py
```

`/analytics/quality` 读的是已有的 `agent_runs` / `job_runs` / `email_logs`，**不读 trace**；
某天没有运行会显示为 0，而不是从序列里消失。它度量的是"系统有没有变差"，不是"答案好不好"——
后者这个仓库没有 rubric，也没有假装有。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | agent 功能必填 | 没有它 agent 直接拒绝运行，不会伪造结果 |
| `OPENAI_BASE_URL` | 可选 | 默认 `https://api.xiaomimimo.com/v1`，兼容 DeepSeek / OpenAI |
| `OPENAI_MODEL` | 可选 | 默认 `mimo-v2.5` |
| `SMTP_*` / `REPORT_EMAIL` | 可选 | 每日报告邮件；不配置则打印到 stdout |
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
