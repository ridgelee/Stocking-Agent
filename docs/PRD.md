# Stock Insight Agent — PRD v2.0 (Final MVP)

**版本**: v2.0  
**日期**: 2025-04-05  
**技术栈**: Python / Django / PostgreSQL / Alpaca MCP / Claude API  
**目标读者**: Vibe Coding Agent  

---

## 0. Vibe Coding Agent 开发路线图

> 请严格按照以下顺序开发，每一步完成后验证后再进入下一步。**不要跳步。**

```
Step 1 → Django 项目初始化 + PostgreSQL 连接 + 数据模型
Step 2 → Module 1: 数据采集层（3个API → NewsArticle表）
Step 3 → Module 2: 结构化提取层（Claude API → 更新NewsArticle字段）
Step 4 → Module 3: 报告生成层（汇总 → DailyReport表）
Step 5 → Module 4: Django API Views（6个endpoint）
Step 6 → Module 5: 前端页面（单文件HTML，4个区域）
Step 7 → Module 6: Chat Agent（Claude + Alpaca MCP集成）
Step 8 → cron job 配置（串联pipeline）
Step 9 → 联调测试（端到端跑通）
```

---

## 1. 项目概述

### 1.1 产品定位
**Stock Insight Agent** 是一个面向个人投资者的每日股票舆情分析系统，核心功能：
1. 每日自动从科技媒体、财经新闻、社交媒体三个板块采集股票相关新闻
2. 通过 Claude AI 进行结构化信息提取，生成分析日报（含持仓投资建议）
3. 支持通过自然语言对话完成股票买卖和仓位查询

### 1.2 MVP 边界声明
- ✅ 单用户本地部署
- ✅ 纸交易（Paper Trading）模式，不操作真实资金
- ✅ 英文新闻为主
- ❌ 不做用户认证系统
- ❌ 不做实时 WebSocket 推送
- ❌ 不做复杂的回测或量化模型

---

## 2. 技术选型

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端框架 | Django 4.2+ | 含 Django Management Commands 跑 pipeline |
| 数据库 | PostgreSQL 15+ | 存储新闻和报告数据 |
| AI 提取/报告 | Anthropic Claude API (claude-sonnet-4) | 结构化提取 + 报告生成 |
| 交易接口 | Alpaca MCP Server | 通过 Claude API 的 mcp_servers 参数集成 |
| 前端 | Django Template + 单文件 HTML | Vanilla JS + Chart.js (CDN) |
| 调度 | cron job (系统级) | 每工作日定时触发 |
| 依赖管理 | pip + requirements.txt | — |

---

## 3. 项目目录结构

```
stock_insight_agent/
│
├── manage.py
├── requirements.txt
├── .env                          # API Keys（不提交 git）
├── .env.example
│
├── config/                       # Django 项目配置
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
│
├── apps/
│   ├── pipeline/                 # 数据采集 + 提取 + 报告生成
│   │   ├── management/
│   │   │   └── commands/
│   │   │       └── run_pipeline.py   # python manage.py run_pipeline
│   │   ├── collector.py          # Module 1: 数据采集
│   │   ├── extractor.py          # Module 2: Claude结构化提取
│   │   ├── reporter.py           # Module 3: 报告生成
│   │   └── models.py             # NewsArticle, DailyReport
│   │
│   └── api/                      # Django Views + URL
│       ├── views.py              # 6个endpoint
│       ├── urls.py
│       └── templates/
│           └── index.html        # 前端单文件
│
└── logs/
    └── pipeline.log
```

---

## 4. 数据模型（PostgreSQL）

### 4.1 NewsArticle 表

```python
class NewsArticle(models.Model):
    # 基础字段（采集阶段填充）
    article_id    = models.UUIDField(default=uuid.uuid4, unique=True)
    source_type   = models.CharField(max_length=20)   # tech_media | financial_news | social_media
    source_name   = models.CharField(max_length=100)  # TechCrunch, Alpha Vantage, Reddit
    title         = models.TextField()
    content       = models.TextField()                # 正文或摘要
    url           = models.URLField(unique=True)
    published_at  = models.DateTimeField()
    fetched_at    = models.DateTimeField(auto_now_add=True)

    # Claude 提取字段（提取阶段填充，初始为 null）
    related_tickers = models.JSONField(default=list)  # ["NVDA", "MSFT"]
    event_type      = models.CharField(max_length=30, null=True)
    # earnings | merger_acquisition | policy | product_launch | market_movement | other
    sentiment       = models.CharField(max_length=10, null=True)
    # Bullish | Bearish | Neutral
    impact_level    = models.CharField(max_length=10, null=True)
    # High | Medium | Low
    one_line_summary = models.TextField(null=True)
    key_entities    = models.JSONField(default=list)  # ["NVIDIA", "Jensen Huang"]
    is_extracted    = models.BooleanField(default=False)

    class Meta:
        ordering = ['-published_at']
        indexes = [
            models.Index(fields=['published_at']),
            models.Index(fields=['impact_level']),
            models.Index(fields=['sentiment']),
        ]
```

### 4.2 DailyReport 表

```python
class DailyReport(models.Model):
    report_date   = models.DateField(unique=True)
    generated_at  = models.DateTimeField(auto_now_add=True)
    report_data   = models.JSONField()   # 完整报告 JSON（结构见 Section 7.2）
    status        = models.CharField(max_length=20, default='completed')
    # completed | failed | generating

    class Meta:
        ordering = ['-report_date']
```

---

## 5. 环境变量（.env）

```bash
# Django
SECRET_KEY=your_django_secret_key
DEBUG=True
DATABASE_URL=postgresql://user:password@localhost:5432/stock_insight

# 数据采集 API Keys
NEWSAPI_KEY=your_newsapi_key
ALPHAVANTAGE_KEY=your_alphavantage_key
REDDIT_CLIENT_ID=your_reddit_client_id
REDDIT_CLIENT_SECRET=your_reddit_client_secret
REDDIT_USER_AGENT=stock-insight-agent/1.0

# Anthropic
ANTHROPIC_API_KEY=your_anthropic_key

# Alpaca（Paper Trading 模式）
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
ALPACA_PAPER_TRADE=true
ALPACA_TOOLSETS=account,trading,stock-data,positions
```

---

## 6. Module 1：数据采集层

**文件**: `apps/pipeline/collector.py`  
**触发方式**: `python manage.py run_pipeline` → 内部调用 `collector.run()`

### 6.1 三个数据源

#### 板块 A — 科技媒体（NewsAPI.org）

| 项目 | 内容 |
|------|------|
| API | NewsAPI.org |
| 免费额度 | 100 req/day |
| 端点 | `GET https://newsapi.org/v2/everything` |
| 关键词 | `AI OR "artificial intelligence" OR semiconductor OR EV OR "electric vehicle"` |
| 时间范围 | 最近 24 小时 |
| 条数 | 10 条 |
| source_type | `tech_media` |

```python
params = {
    "q": 'AI OR semiconductor OR EV stock earnings',
    "language": "en",
    "sortBy": "publishedAt",
    "pageSize": 10,
    "from": (datetime.now() - timedelta(hours=24)).isoformat(),
    "apiKey": settings.NEWSAPI_KEY
}
```

#### 板块 B — 财经新闻（Alpha Vantage News Sentiment）

| 项目 | 内容 |
|------|------|
| API | Alpha Vantage |
| 免费额度 | 25 req/day |
| 端点 | `https://www.alphavantage.co/query?function=NEWS_SENTIMENT` |
| 话题 | `technology,earnings,ipo` |
| 条数 | 10 条 |
| source_type | `financial_news` |
| 特点 | 原生返回 ticker 关联，可辅助校验 Claude 提取结果 |

```python
params = {
    "function": "NEWS_SENTIMENT",
    "topics": "technology,earnings,ipo",
    "limit": 10,
    "apikey": settings.ALPHAVANTAGE_KEY
}
```

#### 板块 C — 社交媒体（Reddit PRAW）

| 项目 | 内容 |
|------|------|
| API | Reddit PRAW |
| 免费额度 | 免费，60 req/min |
| 抓取版块 | r/stocks, r/wallstreetbets, r/investing |
| 排序 | `hot` 前 10 |
| 过滤 | 标题含股票相关词（stock/buy/sell/earnings/market） |
| 条数 | 约 10 条（过滤后） |
| source_type | `social_media` |

### 6.2 采集规则

- **幂等性**: 以 `url` 为唯一键，用 `get_or_create`，重复 URL 跳过不报错
- **容错**: 任一板块采集失败，记录日志并继续其他板块，不中断整体 pipeline
- **当日去重**: 查询今日已采集数量，若已有 20+ 条则跳过采集步骤

### 6.3 输出

将采集结果存入 PostgreSQL `NewsArticle` 表，`is_extracted=False`。

---

## 7. Module 2：结构化提取层

**文件**: `apps/pipeline/extractor.py`

### 7.1 提取字段 Schema

```json
{
  "related_tickers": ["NVDA", "MSFT"],
  "event_type": "earnings",
  "sentiment": "Bullish",
  "impact_level": "High",
  "one_line_summary": "NVIDIA Q4 earnings beat expectations by 20%, driven by AI chip demand",
  "key_entities": ["NVIDIA", "Jensen Huang", "H100", "Blackwell"]
}
```

**字段定义**:

| 字段 | 枚举值 | 说明 |
|------|--------|------|
| `related_tickers` | 任意 ticker | 新闻直接关联的股票代码，最多5个，无则 `[]` |
| `event_type` | `earnings / merger_acquisition / policy / product_launch / market_movement / other` | 六选一 |
| `sentiment` | `Bullish / Bearish / Neutral` | 对关联股票的价格预期方向 |
| `impact_level` | `High / Medium / Low` | High=可能引发>2%价格波动；Low=信息量低 |
| `one_line_summary` | 文本 | 不超过50词，不得复制标题 |
| `key_entities` | 文本列表 | 公司名、人名、产品名，最多5个 |

### 7.2 Claude API 调用规范

**批处理**: 每批 10 条，串行处理  
**模型**: `claude-sonnet-4-20250514`

**System Prompt**:
```
You are a financial news analyst specializing in equity markets.
Extract structured information from news articles for stock market analysis.

STRICT RULES:
- Respond with valid JSON array ONLY. No markdown, no explanation, no fences.
- related_tickers: stock ticker symbols only (e.g. NVDA, not "Nvidia"). Max 5. Use [] if none.
- event_type: must be exactly one of: earnings, merger_acquisition, policy, product_launch, market_movement, other
- sentiment: must be exactly one of: Bullish, Bearish, Neutral. Judge by likely stock price impact.
- impact_level: High (likely >2% move), Medium (notable but limited), Low (minimal market impact)
- one_line_summary: max 50 words, do not copy the title, focus on market implications
- key_entities: company names, people, products only. Max 5. Use [] if none.
```

**User Prompt**:
```
Extract structured information from the following {n} news articles.
Return a JSON array with exactly {n} objects.

Schema for each object:
{
  "related_tickers": [],
  "event_type": "",
  "sentiment": "",
  "impact_level": "",
  "one_line_summary": "",
  "key_entities": []
}

Articles:
{articles_json}
```

**错误处理策略**:

```python
def extract_batch(articles):
    try:
        response = claude_api.call(prompt)
        result = json.loads(response)
        return result
    except json.JSONDecodeError:
        # 第一次失败：重试，prompt末尾加强调
        retry_prompt = prompt + "\nCRITICAL: Return ONLY a valid JSON array. Nothing else."
        try:
            response = claude_api.call(retry_prompt)
            return json.loads(response)
        except:
            # 第二次仍失败：填充默认值，记录日志
            return [default_extraction() for _ in articles]
```

**默认值** (提取失败时):
```python
def default_extraction():
    return {
        "related_tickers": [],
        "event_type": "other",
        "sentiment": "Neutral",
        "impact_level": "Low",
        "one_line_summary": "[Extraction failed]",
        "key_entities": []
    }
```

### 7.3 输出

更新 `NewsArticle` 表中对应记录的提取字段，设置 `is_extracted=True`。

---

## 8. Module 3：报告生成层

**文件**: `apps/pipeline/reporter.py`  
**触发时机**: 提取完成后自动触发；或前端点击"分析报告"按钮时触发

### 8.1 报告生成步骤

**Step 1 — 纯 Python 统计汇总**（不调用 Claude）
```python
# 读取今日所有已提取新闻
articles = NewsArticle.objects.filter(
    published_at__date=today,
    is_extracted=True
)

summary = {
    "total_news_count": articles.count(),
    "high_impact_count": articles.filter(impact_level="High").count(),
    "sentiment_distribution": {
        "Bullish": articles.filter(sentiment="Bullish").count(),
        "Bearish": articles.filter(sentiment="Bearish").count(),
        "Neutral": articles.filter(sentiment="Neutral").count(),
    },
    "event_type_distribution": {...},  # 同理按 event_type 分组计数
}

# Top Tickers：展开 related_tickers JSON数组，统计频次，取前10
top_tickers = compute_ticker_frequency(articles)[:10]

# Top 5 Highlights：impact_level=High 优先，再按 Bullish/Bearish 优先
highlights = select_highlights(articles, limit=5)
```

**Step 2 — 获取实时持仓**（调用 Alpaca MCP）
```python
# 通过 Claude API + mcp_servers 获取持仓数据
positions = fetch_portfolio_via_mcp()
# 返回格式：[{"symbol": "NVDA", "qty": 10, "avg_cost": 800, "current_price": 875, "unrealized_pnl": 750}]
```

**Step 3 — Claude 生成分析文字**（1次 Claude API 调用）

```
System: You are a senior equity analyst. Write concise, factual market analysis.
Always end investment suggestions with: "⚠️ 以上内容仅供参考，不构成投资建议。"

User:
Based on today's news data and my current portfolio, generate a daily market report.

=== Today's Statistics ===
{summary_json}

=== Top 5 News Highlights ===
{highlights_json}

=== My Current Portfolio ===
{positions_json}

Generate a report with exactly these 3 sections:

## 今日市场趋势
[2-3 sentences on today's dominant market theme based on news data]

## 重要事件深度总结
[For each of the top 3 high-impact events: background + market implication, 2-3 sentences each]

## 持仓相关投资参考
[For each position I hold, check if today's news is relevant. 
 If relevant: briefly explain the news impact on that stock.
 If no relevant news: state "今日暂无直接相关新闻".
 End with the mandatory disclaimer.]
```

### 8.2 报告数据结构（存入 DailyReport.report_data）

```json
{
  "date": "2025-04-04",
  "generated_at": "2025-04-04T09:00:00Z",
  "summary": {
    "total_news_count": 28,
    "high_impact_count": 5,
    "sentiment_distribution": {"Bullish": 12, "Bearish": 8, "Neutral": 8},
    "event_type_distribution": {
      "earnings": 6, "product_launch": 7, "policy": 4,
      "merger_acquisition": 3, "market_movement": 5, "other": 3
    }
  },
  "top_tickers": [
    {"ticker": "NVDA", "mention_count": 8, "dominant_sentiment": "Bullish"}
  ],
  "highlights": [
    {
      "rank": 1,
      "title": "事件标题",
      "one_line_summary": "...",
      "related_tickers": ["NVDA"],
      "sentiment": "Bullish",
      "impact_level": "High",
      "source_url": "https://..."
    }
  ],
  "portfolio_snapshot": [
    {
      "symbol": "NVDA",
      "qty": 10,
      "avg_cost": 800.00,
      "current_price": 875.20,
      "market_value": 8752.00,
      "unrealized_pnl": 752.00,
      "unrealized_pnl_pct": 9.4
    }
  ],
  "analysis_text": {
    "market_trend": "今日市场以AI芯片为主题...",
    "event_summary": "1. NVIDIA财报超预期...\n2. ...",
    "portfolio_insight": "您持有的 NVDA...\n\n⚠️ 以上内容仅供参考，不构成投资建议。"
  }
}
```

---

## 9. Module 4：Django API Views

**文件**: `apps/api/views.py` + `apps/api/urls.py`

### 9.1 URL 配置

```python
# config/urls.py
urlpatterns = [
    path('', include('apps.api.urls')),
]

# apps/api/urls.py
urlpatterns = [
    path('', views.index, name='index'),                              # 首页HTML
    path('api/news/', views.news_list, name='news_list'),             # 新闻列表
    path('api/portfolio/', views.portfolio, name='portfolio'),        # 实时持仓
    path('api/report/generate/', views.generate_report, name='generate_report'),  # 触发生成
    path('api/report/latest/', views.latest_report, name='latest_report'),        # 读取报告
    path('api/chat/', views.chat, name='chat'),                       # 对话Agent
]
```

### 9.2 各 Endpoint 规范

---

#### `GET /` — 首页

返回渲染好的 `index.html` Django Template。无参数。

---

#### `GET /api/news/`

从 PostgreSQL 返回今日新闻列表。

**Query Params**:
| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 20 | 最多返回条数 |
| `impact` | string | `all` | `High / Medium / Low / all` |
| `sentiment` | string | `all` | `Bullish / Bearish / Neutral / all` |
| `source_type` | string | `all` | `tech_media / financial_news / social_media / all` |

**Response**:
```json
{
  "count": 20,
  "date": "2025-04-04",
  "articles": [
    {
      "id": "uuid",
      "title": "...",
      "source_name": "TechCrunch",
      "source_type": "tech_media",
      "published_at": "2025-04-04T08:00:00Z",
      "url": "https://...",
      "related_tickers": ["NVDA"],
      "event_type": "product_launch",
      "sentiment": "Bullish",
      "impact_level": "High",
      "one_line_summary": "..."
    }
  ]
}
```

---

#### `GET /api/portfolio/`

实时从 Alpaca MCP 拉取持仓信息。

**实现方式**:
```python
def portfolio(request):
    # 通过 Claude API + mcp_servers 调用 get_all_positions + get_account_info
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system="Return ONLY valid JSON. Call get_all_positions and get_account_info, then format the results.",
        messages=[{"role": "user", "content": "Get my current portfolio positions and account balance. Return as JSON."}],
        mcp_servers=[{
            "type": "url",
            "url": "https://mcp.alpaca.markets/sse",  # 或 stdio 方式
            "name": "alpaca"
        }]
    )
    # 解析返回的 JSON
```

**Response**:
```json
{
  "account": {
    "cash": 15000.00,
    "portfolio_value": 45231.50,
    "buying_power": 15000.00
  },
  "positions": [
    {
      "symbol": "NVDA",
      "qty": 10,
      "avg_cost": 800.00,
      "current_price": 875.20,
      "market_value": 8752.00,
      "unrealized_pnl": 752.00,
      "unrealized_pnl_pct": 9.40,
      "side": "long"
    }
  ]
}
```

**错误处理**: Alpaca MCP 连接失败时返回 `{"error": "Unable to connect to Alpaca", "positions": [], "account": {}}`，前端显示提示而非崩溃。

---

#### `POST /api/report/generate/`

触发报告生成（含获取实时持仓 + Claude 分析）。

**Request Body**: 无需参数  
**行为**: 检查今日是否已有报告，有则直接返回，无则触发生成  
**Response**:
```json
{
  "status": "generated",  // generated | already_exists | generating
  "report_date": "2025-04-04",
  "message": "Report generated successfully"
}
```

> ⚠️ MVP 阶段同步生成（可能需要 10-20 秒），前端展示 loading 状态。

---

#### `GET /api/report/latest/`

读取最新一天的报告数据。

**Response**: 直接返回 `DailyReport.report_data` JSON 内容  
**无报告时**: `{"error": "No report available", "hint": "Click '分析报告' to generate"}` (404)

---

#### `POST /api/chat/`

核心对话 Agent，通过 Claude + Alpaca MCP 实现自然语言交易。

**Request Body**:
```json
{
  "message": "帮我查看我的当前仓位",
  "history": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

**实现**（详见 Module 6）

**Response**:
```json
{
  "reply": "您当前持有以下股票：\n- NVDA: 10股，当前价格 $875.20，浮盈 $752 (+9.4%)\n- AAPL: 5股...",
  "action_type": "query_positions",  // query_positions | place_order | query_account | general
  "requires_confirmation": false
}
```

---

## 10. Module 5：前端页面

**文件**: `apps/api/templates/index.html`（单文件，内嵌所有 CSS + JS）

### 10.1 页面布局

```
┌─────────────────────────────────────────────────────────────┐
│  📈 Stock Insight Agent          [今日 2025-04-04]  [刷新]   │  ← Header
├────────────────────────────┬────────────────────────────────┤
│                            │                                │
│  左侧主区域 (65%)           │  右侧 Chat UI (35%)            │
│                            │                                │
│  ┌──────────────────────┐  │  ┌──────────────────────────┐  │
│  │  💼 我的持仓          │  │  │  💬 交易助手              │  │
│  │  总市值: $45,231      │  │  │                          │  │
│  │  NVDA  10股  +9.4%   │  │  │  [对话记录区域]           │  │
│  │  AAPL   5股  +2.1%   │  │  │                          │  │
│  │  现金: $15,000        │  │  │  Bot: 您好！我可以帮助您   │  │
│  └──────────────────────┘  │  │  查询仓位或执行交易。      │  │
│                            │  │                          │  │
│  [🔍 分析报告] ← 按钮      │  │  User: 买入NVDA 5股       │  │
│                            │  │  Bot: 确认购买？...        │  │
│  ┌──────────────────────┐  │  │                          │  │
│  │  📰 今日新闻 (20条)   │  │  └──────────────────────────┘  │
│  │                      │  │  ┌──────────────────────────┐  │
│  │  [High] Bullish      │  │  │ [输入框...]    [发送 ↵]  │  │
│  │  NVDA earnings beat  │  │  └──────────────────────────┘  │
│  │  ...                 │  │                                │
│  │  [筛选: 全部▼]        │  │  快捷: [查仓位][今日热点]      │
│  └──────────────────────┘  │                                │
│                            │                                │
└────────────────────────────┴────────────────────────────────┘

[分析报告 Modal - 点击按钮后弹出]
┌─────────────────────────────────────────────────────────────┐
│  📊 今日分析报告  ×                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ 情绪分布      │  │ 事件类型      │  │ Ticker热度    │      │
│  │ [环形图]      │  │ [柱状图]      │  │ [条形图]      │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
│  ## 今日市场趋势                                             │
│  今日市场以AI芯片为主题...                                    │
│                                                             │
│  ## 重要事件深度总结                                         │
│  1. NVIDIA财报超预期...                                      │
│                                                             │
│  ## 持仓相关投资参考                                         │
│  您持有的 NVDA: 今日有重大利好消息...                         │
│  ⚠️ 以上内容仅供参考，不构成投资建议。                        │
└─────────────────────────────────────────────────────────────┘
```

### 10.2 前端数据加载逻辑

```javascript
// 页面加载时并发请求（不互相阻塞）
async function init() {
    // 两个请求并发
    const [newsRes, portfolioRes] = await Promise.allSettled([
        fetch('/api/news/?limit=20'),
        fetch('/api/portfolio/')
    ]);
    
    if (newsRes.status === 'fulfilled') renderNews(await newsRes.value.json());
    if (portfolioRes.status === 'fulfilled') renderPortfolio(await portfolioRes.value.json());
}

// 点击"分析报告"按钮
async function openReport() {
    showModal();
    showLoading('正在生成分析报告，请稍候...');
    
    // 先触发生成（如已存在直接返回）
    await fetch('/api/report/generate/', { method: 'POST' });
    
    // 再获取报告数据
    const report = await fetch('/api/report/latest/').then(r => r.json());
    renderReport(report);
}
```

### 10.3 图表规范（Chart.js CDN）

| 图表 | 类型 | 数据字段 |
|------|------|---------|
| 情绪分布 | Doughnut（环形） | `report.summary.sentiment_distribution` |
| 事件类型 | Horizontal Bar | `report.summary.event_type_distribution` |
| Ticker 热度 | Horizontal Bar | `report.top_tickers`（前8个） |

### 10.4 新闻列表渲染

每条新闻显示：
- 标题（可点击跳转原文）
- 来源 + 发布时间
- 情绪标签（色块：Bullish=绿，Bearish=红，Neutral=灰）
- 影响评级标签（High=橙，Medium=蓝，Low=灰）
- 相关 Ticker 标签
- 一句话摘要

顶部筛选栏：按情绪、影响级别、板块类型筛选（前端 JS 实现，不重新请求后端）

### 10.5 持仓信息渲染

显示内容：
- 账户总市值 + 现金余额
- 每只持仓：股票代码 + 数量 + 当前价格 + 盈亏金额 + 盈亏百分比（红绿色区分）
- 若 Alpaca MCP 不可用：显示 "⚠️ 持仓数据暂不可用" 提示

---

## 11. Module 6：Chat Agent（Claude + Alpaca MCP）

**文件**: `apps/api/views.py` 中的 `chat` view

### 11.1 架构说明

```
前端 Chat UI
    → POST /api/chat/
        → Django View
            → Anthropic Python SDK
                → model: claude-sonnet-4
                → mcp_servers: [Alpaca MCP Server]
                    → Claude 自动调用 MCP 工具
                        (get_all_positions / place_stock_order / get_account_info 等)
            ← Claude 自然语言回复
        ← JSON Response
    ← 显示在 Chat UI
```

### 11.2 Alpaca MCP Server 配置

```python
# Alpaca MCP Server 通过 uvx 启动（stdio 模式）
# 在 Django view 中通过 Anthropic SDK 的 mcp_servers 参数接入

mcp_server_config = {
    "type": "stdio",
    "command": "uvx",
    "args": ["alpaca-mcp-server"],
    "env": {
        "ALPACA_API_KEY": settings.ALPACA_API_KEY,
        "ALPACA_SECRET_KEY": settings.ALPACA_SECRET_KEY,
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,trading,stock-data,positions"
    }
}
```

### 11.3 Agent System Prompt

```
You are a stock trading assistant integrated with Alpaca Paper Trading.
You help users check their portfolio and execute trades through natural conversation.

YOUR CAPABILITIES:
- Query current positions and account balance (get_all_positions, get_account_info)
- Buy and sell stocks (place_stock_order)
- Close positions (close_position)
- Get stock market data (get_stock_snapshot)

STRICT SAFETY RULES:
1. ALWAYS confirm before executing ANY trade
   - Show: symbol, quantity, estimated price, order type
   - Wait for explicit confirmation words: "确认" / "是" / "yes" / "confirm"
   - Do NOT execute if user says "算了" / "取消" / "no" / "cancel"
2. This is PAPER TRADING - make this clear if user asks
3. You are an EXECUTION assistant, NOT a financial advisor
   - Never suggest which stocks to buy
   - Never predict price movements
4. If the market is closed, inform the user but still allow order placement (GTC orders)

RESPONSE FORMAT:
- Keep responses concise and clear
- Use Chinese for Chinese messages, English for English
- For trade confirmations, always format as:
  "确认下单信息：
   操作：[买入/卖出]
   股票：[TICKER]
   数量：[X] 股
   类型：市价单
   请回复「确认」执行，或「取消」放弃。"
```

### 11.4 功能边界

| 用户输入示例 | Agent 行为 |
|------------|-----------|
| "查看我的仓位" | 调用 `get_all_positions` + `get_account_info`，格式化展示 |
| "买入 NVDA 10股" | 先返回确认信息，等待用户"确认"后调用 `place_stock_order` |
| "卖出我的 TSLA" | 先调用 `get_open_position(TSLA)` 确认数量，返回确认信息 |
| "清空所有持仓" | 明确警告后，返回二次确认要求 |
| "NVDA 现在多少钱" | 调用 `get_stock_snapshot`，返回最新价格 |
| "你觉得应该买什么" | 拒绝，说明自己只是执行助手 |

### 11.5 对话历史管理

- 历史消息存储在前端 `chatHistory` 数组（JavaScript 内存）
- 每次请求将完整历史发送至后端
- 后端传给 Claude API 的 `messages` 参数包含完整历史
- MVP 阶段不持久化对话历史到数据库

---

## 12. 调度配置（cron job）

### 12.1 Django Management Command

```python
# apps/pipeline/management/commands/run_pipeline.py

class Command(BaseCommand):
    help = 'Run the daily news pipeline: collect → extract → store'

    def handle(self, *args, **options):
        self.stdout.write(f'[{datetime.now()}] Starting pipeline...')
        
        # Step 1: 采集
        collector.run()
        
        # Step 2: 结构化提取（仅处理未提取的）
        extractor.run()
        
        self.stdout.write(f'[{datetime.now()}] Pipeline completed.')
        # 注意：报告生成由用户点击按钮触发，pipeline不自动生成报告
```

### 12.2 cron 配置

```bash
# 每个工作日早上 7:30 AM (系统时间，按需调整时区)
30 7 * * 1-5 cd /path/to/project && python manage.py run_pipeline >> logs/pipeline.log 2>&1
```

### 12.3 完整执行时序

```
07:30 AM  cron 触发 run_pipeline
           ├── collector.run()    采集3个板块，写入 NewsArticle
           └── extractor.run()   Claude API 批量提取，更新 NewsArticle

用户访问首页
           ├── GET /api/news/     从 PostgreSQL 读取今日新闻（已提取）
           └── GET /api/portfolio/ 实时调用 Alpaca MCP 获取持仓

用户点击「分析报告」
           └── POST /api/report/generate/
               ├── 聚合统计数据（Python）
               ├── 获取实时持仓（Alpaca MCP）
               ├── Claude 生成分析文字（含持仓维度）
               └── 存入 DailyReport 表，返回前端渲染
```

---

## 13. requirements.txt

```
Django==4.2.10
psycopg2-binary==2.9.9
python-dotenv==1.0.1
anthropic==0.20.0
requests==2.31.0
praw==7.7.1
django-cors-headers==4.3.1
```

---

## 14. MVP 开发里程碑与验收标准

| 里程碑 | 任务 | 验收标准 |
|--------|------|----------|
| **M1** | Django 初始化 + DB 连接 | `python manage.py migrate` 成功，PostgreSQL 中出现两张表 |
| **M2** | 数据采集层 | `python manage.py run_pipeline` 后 `NewsArticle.objects.count() >= 15` |
| **M3** | 结构化提取层 | 所有 `is_extracted=False` 的记录被处理，`sentiment`字段有值 |
| **M4** | API Views | `GET /api/news/` 返回20条新闻 JSON；`GET /api/portfolio/` 返回持仓数据 |
| **M5** | 前端首页 | 打开 `http://localhost:8000`，新闻列表和持仓信息正常显示 |
| **M6** | 分析报告 | 点击"分析报告"按钮，10-20秒内弹出含图表和分析文字的 Modal |
| **M7** | Chat Agent | 输入"查看我的仓位"，收到正确持仓信息；输入"买入NVDA 5股"，收到确认提示 |
| **M8** | 联调 | cron job 触发，隔天新闻自动更新，完整链路无人工干预跑通 |

---

## 15. 关键技术决策说明

### 为什么选 Django 而非 FastAPI？
Django 自带 Management Commands，非常适合跑定时 pipeline，无需额外任务队列。ORM + Admin 也方便 Debug 阶段直接查看数据。MVP 阶段复杂度是可控的。

### 为什么持仓数据不存 PostgreSQL？
仓位是实时数据，存 DB 需要同步机制，引入复杂度。MVP 直接实时拉取，简单可靠，响应时间在可接受范围（1-3秒）。

### 为什么报告生成是按需触发而非自动？
报告生成需要实时持仓数据（Alpaca MCP），且包含 Claude API 调用。cron 阶段只做数据采集和提取，报告由用户主动触发，确保持仓数据是当时最新的。

### Alpaca MCP 的接入方式
通过 Anthropic Python SDK 的 `mcp_servers` 参数，以 stdio 方式启动 Alpaca MCP Server，让 Claude 直接调用 MCP 工具。Django View 不直接调用 Alpaca REST API，所有交易操作由 Claude 通过 MCP 完成。

### 分析报告的投资建议
明确标注「⚠️ 以上内容仅供参考，不构成投资建议。」，此为 System Prompt 强制要求，Claude 每次生成持仓分析时必须附加此免责声明。

---

## 16. 已知限制（MVP 阶段可接受）

- 报告生成同步执行，可能需要 10-20 秒（后续可改为异步任务）
- 对话历史不持久化，刷新页面后对话重置
- NewsAPI 免费版每天 100 次请求，Alpha Vantage 25 次请求
- 前端无骨架屏，持仓加载期间显示 Loading 文字

---

*文档结束。如有任何架构调整，请在动工前先修改此 PRD 并与 Lee 确认。*
