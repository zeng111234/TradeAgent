# TradeAgent - AI外贸智能获客助手

> 基于Python + FastAPI + Vue3的外贸客户开发与邮件营销工具

## 项目背景

针对外贸业务员在使用富通天下等平台时的痛点，打造一个更智能、更低成本的获客与客户管理工具。

## 核心功能

### 1. 智能客户开发（获客）
- 从公开数据源（海关数据、行业目录）获取潜在买家信息
- AI分析买家采购历史，评估匹配度
- 自动生成个性化英文开发信

### 2. 邮件营销自动化
- 邮件模板管理（支持变量替换：公司名、联系人、产品等）
- 批量发送 + 定时发送
- 邮件打开/回复追踪
- AI自动分类回复（询价/拒绝/待跟进/成交）

### 3. 客户CRM管理
- 客户标签分类（按行业/地区/采购意向/跟进阶段）
- 跟进提醒和待办事项
- 客户沟通时间线
- 报价单生成

### 4. 数据分析看板
- 获客转化漏斗
- 邮件打开率/回复率统计
- 客户来源分析
- 销售预测

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.11 + FastAPI |
| 数据库 | SQLite（开发） / PostgreSQL（生产） |
| ORM | SQLAlchemy |
| 前端 | Vue3 + Vite + Element Plus |
| AI | OpenAI API |
| 爬虫 | requests + BeautifulSoup |
| 部署 | Docker + docker-compose |

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

### Docker一键启动
```bash
docker-compose up -d
```

## 项目结构

```
Foreign Trade/
├── backend/                    # Python后端
│   ├── app/
│   │   ├── main.py            # FastAPI入口
│   │   ├── config.py          # 配置管理
│   │   ├── database.py        # 数据库连接
│   │   ├── models/            # 数据库模型
│   │   ├── routers/           # API路由
│   │   ├── services/          # 业务逻辑
│   │   └── utils/             # 工具函数
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                   # Vue3前端
│   ├── src/
│   │   ├── views/             # 页面组件
│   │   ├── components/        # 公共组件
│   │   ├── api/               # API请求封装
│   │   └── router/            # 路由配置
│   └── package.json
├── docker-compose.yml
└── README.md
```

## 作者

GitHub: [@zeng111234](https://github.com/zeng111234)

## License

MIT