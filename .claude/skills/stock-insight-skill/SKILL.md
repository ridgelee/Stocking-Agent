---
name: stock-insight-agent-dev
description: |
  Step-by-step VibeCoding development guide for the Stock Insight Agent MVP.
  Use this skill whenever the user says "开始开发", "继续开发", "下一步", "build the stock agent",
  or references any step number (Step 1–9) from the Stock Insight Agent project.
  Also trigger when the user mentions Django setup, news pipeline, Claude extraction,
  Alpaca MCP chat, report generation, or any module of this specific project.
  This skill orchestrates the FULL development lifecycle using one subagent per step,
  enforces schema + report requirements from the VibeCoding rubric, and maintains
  an AI usage log at /Users/howyoulee/Daily AI Insight Engine/docs/AI使用报告.
---

# Stock Insight Agent — VibeCoding Development Skill

## 0. 你的第一件事

读取完整 PRD 参考文件：
```
cat /mnt/skills/user/stock-insight-agent-dev/references/PRD.md
```

然后确认用户想从哪个 Step 开始（默认从 Step 1 开始）。**每个 Step 用独立 subagent 执行。**

---

## 1. 开发总路线图

```
Step 1 → Django 初始化 + PostgreSQL + 数据模型
Step 2 → 数据采集层（3个API → NewsArticle表）
Step 3 → 结构化提取层（Claude API → Schema）    ← 核心考察点
Step 4 → 报告生成层（汇总 + Claude分析）         ← 核心考察点
Step 5 → Django API Views（6个endpoint）
Step 6 → 前端页面（单文件HTML + Chat UI）
Step 7 → Chat Agent（Claude + Alpaca MCP）
Step 8 → cron job 配置
Step 9 → 联调测试 + AI使用报告更新
```

**规则：每步完成验收后才能进入下一步。不跳步。**

---

## 2. Subagent 执行协议

每个 Step 启动时，用以下格式 spawn subagent：

```
SUBAGENT TASK: Step N — [Step名称]
PROJECT ROOT: ~/stock_insight_agent/
VERIFY: [具体验收指令]
ON SUCCESS: 报告完成 + 输出验收结果
ON FAIL: 报告错误 + 建议修复方案，不自动进入下一步
```

主 agent 等待 subagent 完成后：
1. 展示验收结果给用户
2. 更新 AI 使用报告（见 Section 6）
3. 询问用户确认进入下一步

---

## 3. 每个 Step 的详细指令

### Step 1 — Django 初始化 + PostgreSQL + 数据模型

**Subagent 任务**:
```bash
# 1. 创建项目结构
django-admin startproject config .
python manage.py startapp pipeline  # 放入 apps/pipeline/
python manage.py startapp api       # 放入 apps/api/

# 2. 配置 settings.py
# - INSTALLED_APPS 加入 'apps.pipeline', 'apps.api'
# - DATABASES 配置 PostgreSQL (读取 .env 的 DATABASE_URL)
# - 使用 python-dotenv 加载 .env

# 3. 创建数据模型（apps/pipeline/models.py）
# 严格按照 PRD Section 4 的字段定义
# NewsArticle + DailyReport 两张表

# 4. 运行迁移
python manage.py makemigrations
python manage.py migrate
```

**验收指令**:
```bash
python manage.py shell -c "from apps.pipeline.models import NewsArticle, DailyReport; print('Models OK:', NewsArticle._meta.fields)"
```
**验收标准**: 输出包含 `sentiment`, `impact_level`, `is_extracted` 等字段，无报错。

**关键文件清单**:
- `config/settings.py` — 含 DB 配置
- `apps/pipeline/models.py` — NewsArticle + DailyReport
- `.env` — 包含所有 API Keys（参考 PRD Section 5）
- `requirements.txt` — 见 PRD Section 13

---

### Step 2 — 数据采集层

**Subagent 任务**: 实现 `apps/pipeline/collector.py`

三个采集函数，全部写入 NewsArticle 表：

```python
# collector.py 必须包含：
def collect_tech_media():
    """NewsAPI.org — 科技板块"""
    # keywords: AI OR semiconductor OR EV
    # pageSize: 10, language: en, last 24h
    # source_type = "tech_media"

def collect_financial_news():
    """Alpha Vantage News Sentiment — 财经板块"""
    # topics: technology,earnings,ipo
    # limit: 10
    # source_type = "financial_news"
    # 注意：alpha vantage 原生返回 ticker，存入 raw_tickers 备用

def collect_social_media():
    """Reddit PRAW — 社交媒体板块"""
    # subreddits: stocks, wallstreetbets, investing
    # sort: hot, top 10
    # 过滤：标题含 stock/buy/sell/earnings/market
    # source_type = "social_media"

def run():
    """主入口，串行执行三个采集，任一失败记录日志不中断"""
    # 幂等：url 为唯一键，使用 get_or_create
    # 今日已有 20+ 条则跳过
```

**验收指令**:
```bash
python manage.py shell -c "
from apps.pipeline.collector import run
run()
from apps.pipeline.models import NewsArticle
print('Count:', NewsArticle.objects.count())
print('Sources:', list(NewsArticle.objects.values('source_type').distinct()))
"
```
**验收标准**: count >= 15，三种 source_type 均出现。

---

### Step 3 — 结构化提取层 ⭐ 核心考察点

> ⚠️ 这是 VibeCoding 评分的核心模块。必须体现「结构化抽取」，不允许只做摘要。

**Subagent 任务**: 实现 `apps/pipeline/extractor.py`

#### 3.1 Schema 设计（必须在代码注释中说明设计思路）

```python
"""
Schema 设计思路说明（必须写入代码顶部注释）：

为什么这样设计字段？

1. related_tickers（JSON Array）
   - 原因：不同新闻来源对同一公司的指代方式不同（"Nvidia" vs "NVDA" vs "the chip maker"）
   - 结构化好处：后续可直接按 ticker 聚合，计算每只股票的舆情热度
   
2. event_type（Enum: 6类）
   - 原因：投资决策中不同事件的权重不同。财报 > 并购 > 政策 > 产品发布
   - 不做摘要而做分类的原因：分类后可做统计（哪类事件今天最多）
   
3. sentiment（Bullish/Bearish/Neutral）
   - 原因：情绪是对"此新闻对股价的预期影响"的判断，不是新闻本身的语气
   - 例：一篇写"监管机构调查苹果"的新闻语气可能是中立的，但 sentiment = Bearish
   
4. impact_level（High/Medium/Low）
   - 原因：量化新闻的市场重要性。High = 可能引发 >2% 股价波动
   - 用于前端优先展示高影响事件
   
5. one_line_summary（Text, max 50 words）
   - 不是原文摘要，而是"市场含义摘要"：这件事对投资者意味着什么？
   - 必须比原标题包含更多市场解读信息
   
6. key_entities（JSON Array）
   - 提取人名/公司名/产品名，用于跨新闻关联分析
"""
```

#### 3.2 Claude API 调用实现

```python
SYSTEM_PROMPT = """
You are a financial news analyst specializing in equity markets.
Extract structured information from news articles for stock market analysis.

STRICT RULES:
- Respond with valid JSON array ONLY. No markdown, no explanation, no fences.
- related_tickers: stock ticker symbols only (e.g. NVDA, not "Nvidia"). Max 5. Use [] if none.
- event_type: MUST be exactly one of: earnings, merger_acquisition, policy, product_launch, market_movement, other
- sentiment: MUST be exactly one of: Bullish, Bearish, Neutral
  Judge by likely stock PRICE impact, not article tone.
- impact_level: High (likely >2% price move), Medium, Low
- one_line_summary: max 50 words, explain MARKET IMPLICATIONS not just facts
- key_entities: company names, people, products only. Max 5. Use [] if none.
"""

def extract_batch(articles: list) -> list:
    """批量提取，10条一批"""
    # 构造 user prompt（见 PRD Section 7.2）
    # 错误处理：
    # 1. json.JSONDecodeError → 重试一次（加强调语气）
    # 2. 第二次失败 → 填充 default_extraction()，记录日志
    # 3. Claude API 超时 → 等待5秒重试，最多2次

def default_extraction() -> dict:
    return {
        "related_tickers": [],
        "event_type": "other",
        "sentiment": "Neutral",
        "impact_level": "Low",
        "one_line_summary": "[Extraction failed - using defaults]",
        "key_entities": []
    }

def run():
    """只处理 is_extracted=False 的记录"""
```

**验收指令**:
```bash
python manage.py shell -c "
from apps.pipeline.extractor import run
run()
from apps.pipeline.models import NewsArticle
extracted = NewsArticle.objects.filter(is_extracted=True)
print('Extracted:', extracted.count())
sample = extracted.first()
print('Sample sentiment:', sample.sentiment)
print('Sample tickers:', sample.related_tickers)
print('Sample event_type:', sample.event_type)
"
```
**验收标准**: extracted.count() > 0，sentiment/event_type/related_tickers 字段均有非空值。

---

### Step 4 — 报告生成层 ⭐ 核心考察点

**Subagent 任务**: 实现 `apps/pipeline/reporter.py`

#### 4.1 三步生成流程（严格顺序）

```python
def run(date=None):
    """
    Step A: Python 统计汇总（不调用 AI）
    Step B: 获取实时持仓（Alpaca MCP）
    Step C: Claude 生成分析文字（1次 API 调用）
    Step D: 存入 DailyReport 表
    """
```

#### 4.2 Step A — 统计汇总（纯 Python）

```python
def compute_summary(articles):
    return {
        "total_news_count": ...,
        "high_impact_count": ...,
        "sentiment_distribution": {"Bullish": x, "Bearish": y, "Neutral": z},
        "event_type_distribution": {type: count for each type},
        "top_tickers": compute_ticker_frequency(articles)[:10],
        "highlights": select_top_highlights(articles, limit=5)
        # 优先级：impact_level=High > Bullish/Bearish > Neutral
    }
```

#### 4.3 Step B — 获取持仓（Alpaca MCP）

```python
def fetch_portfolio():
    """
    通过 Anthropic SDK 的 mcp_servers 参数调用 Alpaca MCP
    返回格式：
    [{"symbol": "NVDA", "qty": 10, "avg_cost": 800, 
      "current_price": 875, "market_value": 8750, 
      "unrealized_pnl": 750, "unrealized_pnl_pct": 9.4}]
    失败时返回 []，记录日志，不中断报告生成
    """
```

#### 4.4 Step C — Claude 生成分析（1次调用）

分析报告必须包含以下三个章节，缺一不可：

```
## 今日市场趋势
[基于统计数据，描述今日主导市场主题，2-3句]

## 重要事件深度总结
[Top 3-5 重要事件，每个事件：背景 + 市场影响，各2-3句]
[要求：有逻辑支撑，不允许空洞描述如"影响较大"/"值得关注"]
[示例：NVIDIA Q4 财报超预期20%，主要由数据中心H100需求驱动。
 短期来看，本次财报可能推动AI基础设施投资继续升温，
 同时对AMD和Intel形成竞争压力...]

## 持仓相关投资参考
[对用户持有的每只股票：]
[- 如有相关新闻：解释新闻对该股票的潜在影响]
[- 如无相关新闻：明确写"今日暂无直接相关新闻"]
[必须以以下免责声明结尾：]
⚠️ 以上内容仅供参考，不构成投资建议。
```

**System Prompt 关键指令**:
```
MANDATORY: End the "持仓相关投资参考" section with exactly:
"⚠️ 以上内容仅供参考，不构成投资建议。"
NEVER make specific buy/sell recommendations.
Analysis must be based on the provided news data, not speculation.
```

**验收指令**:
```bash
python manage.py shell -c "
from apps.pipeline.reporter import run
run()
from apps.pipeline.models import DailyReport
import json
report = DailyReport.objects.latest('report_date')
data = report.report_data
print('Sections:', list(data['analysis_text'].keys()))
print('Has disclaimer:', '仅供参考' in data['analysis_text']['portfolio_insight'])
print('Top tickers:', data['top_tickers'][:3])
"
```
**验收标准**: analysis_text 包含三个章节，portfolio_insight 包含免责声明。

---

### Step 5 — Django API Views

**Subagent 任务**: 实现 `apps/api/views.py` 和 `apps/api/urls.py`

六个 endpoint（详见 PRD Section 9）：

```python
# GET  /              → TemplateView → index.html
# GET  /api/news/     → 新闻列表（支持 impact/sentiment/source_type 过滤）
# GET  /api/portfolio/ → 实时持仓（Alpaca MCP）
# POST /api/report/generate/ → 触发报告生成
# GET  /api/report/latest/   → 读取最新报告
# POST /api/chat/    → Chat Agent（见 Step 7）
```

**验收指令**:
```bash
# 启动 dev server
python manage.py runserver &
sleep 2

# 测试各 endpoint
curl -s http://localhost:8000/api/news/?limit=5 | python -m json.tool | head -20
curl -s http://localhost:8000/api/portfolio/ | python -m json.tool
curl -s http://localhost:8000/api/report/latest/ | python -m json.tool | head -10
```
**验收标准**: 三个 endpoint 均返回有效 JSON，无 500 错误。

---

### Step 6 — 前端页面

**Subagent 任务**: 实现 `apps/api/templates/index.html`（单文件，内嵌所有 CSS + JS）

**四个区域**（详见 PRD Section 10.1 布局图）：

```
区域A — 持仓信息卡片（左上）
  - 账户总市值 + 现金余额
  - 每只持仓：代码 + 数量 + 价格 + 盈亏（红绿色）
  - 数据来源：GET /api/portfolio/（页面加载时调用）

区域B — [分析报告] 按钮（持仓卡片下方）
  - 点击 → POST /api/report/generate/ → loading → GET /api/report/latest/
  - 弹出 Modal，包含三个 Chart.js 图表 + 三章分析文字

区域C — 今日新闻列表（左侧主区域）
  - 默认展示 20 条，按 impact_level 降序
  - 每条显示：标题(可点击) + 来源 + 时间 + 情绪标签 + 影响级别标签 + Ticker标签 + 摘要
  - 顶部筛选栏：情绪 / 影响级别 / 板块（前端 JS 过滤，不重新请求）

区域D — Chat UI（右侧，35%宽）
  - 对话记录区 + 输入框 + 发送按钮
  - Enter 键也可发送
  - loading 动画（"..."）
  - 快捷按钮：「查看我的仓位」「今日高影响事件」
```

**图表规范（Chart.js CDN）**：
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<!-- 三个图表 -->
<!-- 1. Doughnut: sentiment_distribution -->
<!-- 2. Horizontal Bar: event_type_distribution -->
<!-- 3. Horizontal Bar: top_tickers（前8个） -->
```

**验收指令**:
```
1. 打开 http://localhost:8000
2. 确认：持仓区域显示数据（或 "⚠️ 持仓数据暂不可用"）
3. 确认：新闻列表显示 >= 10 条，每条有情绪标签和影响级别标签
4. 点击「分析报告」按钮，确认弹出 Modal 并包含图表
5. 在 Chat UI 输入任意文字，确认能发送（即使 /api/chat/ 返回 mock）
```

---

### Step 7 — Chat Agent（Claude + Alpaca MCP）

**Subagent 任务**: 实现 `apps/api/views.py` 中的 `chat` view

**核心实现**:

```python
def chat(request):
    """
    接收：{"message": str, "history": list}
    调用：Anthropic SDK，mcp_servers=[Alpaca MCP (stdio)]
    返回：{"reply": str, "action_type": str, "requires_confirmation": bool}
    """
    
    # Alpaca MCP 配置（stdio 模式）
    mcp_config = {
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

**System Prompt（完整版）**:
```
You are a stock trading assistant integrated with Alpaca Paper Trading.
You help users check their portfolio and execute trades through natural conversation.

YOUR CAPABILITIES:
- Query current positions and account balance
- Buy and sell stocks (place_stock_order)
- Close positions (close_position)
- Get stock market data (get_stock_snapshot)

STRICT SAFETY RULES:
1. ALWAYS confirm before executing ANY trade:
   Show: symbol, quantity, estimated price, order type
   Wait for: "确认" / "是" / "yes" / "confirm"
   Cancel if: "算了" / "取消" / "no" / "cancel"
   
2. Trade confirmation format (ALWAYS use this):
   "确认下单信息：
    操作：[买入/卖出]
    股票：[TICKER]
    数量：[X] 股
    类型：市价单
    请回复「确认」执行，或「取消」放弃。"

3. This is PAPER TRADING — state this if user asks
4. You are an EXECUTION assistant, NOT a financial advisor
   Never suggest which stocks to buy or predict prices
```

**验收**:
```bash
# 测试1：查仓位
curl -X POST http://localhost:8000/api/chat/ \
  -H "Content-Type: application/json" \
  -d '{"message": "查看我的仓位", "history": []}'
# 期望：返回持仓列表或账户信息

# 测试2：买入确认流程
curl -X POST http://localhost:8000/api/chat/ \
  -H "Content-Type: application/json" \
  -d '{"message": "买入NVDA 5股", "history": []}'
# 期望：返回确认信息（不直接下单）
```

---

### Step 8 — cron job 配置

**Subagent 任务**:

```bash
# 1. 确认 management command 可用
python manage.py run_pipeline --help

# 2. 添加 cron job（工作日 7:30 AM）
crontab -e
# 添加：
# 30 7 * * 1-5 cd /path/to/stock_insight_agent && python manage.py run_pipeline >> logs/pipeline.log 2>&1

# 3. 验证 cron 已添加
crontab -l | grep run_pipeline
```

**注意**: pipeline 只运行 collect + extract，**不生成报告**（报告由用户点击按钮触发）。

---

### Step 9 — 联调测试 + AI使用报告更新

**Subagent 任务（两个并发 subagent）**:

**Subagent A — 端到端测试**:
```bash
# 1. 清空今日数据，重新跑完整 pipeline
python manage.py run_pipeline

# 2. 启动服务
python manage.py runserver

# 3. 测试完整用户流程
# - 打开首页，验证新闻和持仓显示
# - 点击分析报告，验证报告内容
# - 在 Chat UI 完成一次仓位查询
# - 在 Chat UI 完成一次买入确认流程（不需要真正确认）
```

**Subagent B — 更新 AI 使用报告**（见 Section 6）

---

## 4. 关键约束（每步 subagent 必须遵守）

### 4.1 Schema 必须说明设计思路
每个提取字段的选择都必须有注释解释：为什么选这个字段？为什么用枚举而不是自由文本？

### 4.2 分析报告必须有逻辑支撑
Claude 生成的分析文字中：
- ❌ 禁止：「此事件可能对市场产生较大影响」（空洞）
- ✅ 要求：「NVIDIA Q4 数据中心收入同比增长 409%，超市场预期 20%，短期可能...」（有数据支撑）

### 4.3 报告必须含三章节
`market_trend` + `event_summary` + `portfolio_insight`，缺一不可。

### 4.4 免责声明强制写入
portfolio_insight 末尾必须包含：`⚠️ 以上内容仅供参考，不构成投资建议。`

### 4.5 错误处理不能 crash
任何 API 调用失败（新闻 API / Claude API / Alpaca MCP）均需 try-catch，记录日志，返回降级结果。

---

## 5. 验收检查清单（整体）

完成所有 Step 后，逐条确认：

```
[ ] NewsArticle 表有 >= 15 条今日新闻，三种 source_type 均有
[ ] 所有新闻的 sentiment / event_type / impact_level 字段有值
[ ] DailyReport 表有今日报告，analysis_text 含三个章节
[ ] portfolio_insight 末尾含免责声明
[ ] GET /api/news/ 返回 JSON，支持过滤参数
[ ] GET /api/portfolio/ 返回持仓数据（或优雅降级）
[ ] POST /api/report/generate/ 成功触发生成
[ ] 前端首页显示新闻列表 + 持仓信息
[ ] 分析报告 Modal 包含三个图表 + 三章文字
[ ] Chat UI 可发送消息，买入指令返回确认提示而非直接执行
[ ] cron job 已配置，运行 crontab -l 可见
[ ] AI 使用报告已更新（见 Section 6）
```

---

## 6. AI 使用报告更新规范

**文件路径**: `/Users/howyoulee/Daily AI Insight Engine/docs/AI使用报告`

**触发时机**: 每个 Step 完成后，或用户明确要求更新时。

**Subagent 任务格式**:

```
SUBAGENT TASK: 更新 AI 使用报告
FILE: /Users/howyoulee/Daily AI Insight Engine/docs/AI使用报告
ACTION: Append 本次开发记录到文件末尾
```

**写入内容模板**（每次追加，不覆盖）:

```markdown
---
## VibeCoding 开发记录 — [Step N: Step名称]
**时间**: [当前时间]

### 1. 哪些步骤使用了 AI？
- [列举：数据采集 / 结构化提取 / 报告分析 / 代码生成 / 错误修复 / 等]

### 2. Prompt 设计思路
**关键 Prompt 片段**:
```
[粘贴本步骤最核心的 System Prompt 或 User Prompt]
```
**设计原因**:
- [为什么加 STRICT RULES / 为什么用 JSON-only 输出 / 等]

### 3. AI 输出错误处理
- **遇到的问题**: [JSON 解析失败 / 字段为空 / API 超时 / 等，如无则写"本步骤无 AI 输出错误"]
- **处理方式**: [重试机制 / 默认值 fallback / 日志记录]
- **人工介入边界**: [何种情况需要人工检查]

### 4. 成本与效率考量
- **本步骤 Claude API 调用次数**: [N 次]
- **预估 Token 消耗**: 
  - 提取层：~[X] tokens/条 × [N] 条 = ~[总计]
  - 报告层：~[X] tokens/次
- **优化空间**: [是否可 batch / 是否可缓存 / 等]
---
```

---

## 7. 快速参考

### 关键文件位置
```
PRD 完整版:           /mnt/skills/user/stock-insight-agent-dev/references/PRD.md
AI 使用报告:          /Users/howyoulee/Daily AI Insight Engine/docs/AI使用报告
项目根目录:            ~/stock_insight_agent/
Pipeline log:         ~/stock_insight_agent/logs/pipeline.log
```

### 常用调试命令
```bash
# 查看今日新闻数量
python manage.py shell -c "from apps.pipeline.models import NewsArticle; print(NewsArticle.objects.count())"

# 查看提取完成率
python manage.py shell -c "from apps.pipeline.models import NewsArticle; print(NewsArticle.objects.filter(is_extracted=True).count())"

# 手动触发 pipeline
python manage.py run_pipeline

# 查看最新报告
python manage.py shell -c "from apps.pipeline.models import DailyReport; import json; r=DailyReport.objects.latest('report_date'); print(json.dumps(r.report_data, indent=2, ensure_ascii=False)[:500])"
```

### Alpaca MCP 可用工具（本项目用到的）
| 工具 | 用途 |
|------|------|
| `get_all_positions` | 查询所有持仓 |
| `get_account_info` | 查询账户余额和买入能力 |
| `place_stock_order` | 下股票市价/限价单 |
| `close_position` | 平某只股票仓位 |
| `get_stock_snapshot` | 获取股票最新价格快照 |

### 技术栈版本
```
Django==4.2.10
psycopg2-binary==2.9.9
python-dotenv==1.0.1
anthropic==0.20.0
requests==2.31.0
praw==7.7.1
django-cors-headers==4.3.1
```
