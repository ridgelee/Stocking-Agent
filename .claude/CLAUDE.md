# CLAUDE.md — Stock Insight Agent

## 项目简介

面向个人投资者的每日股票舆情分析系统。三路新闻采集 → Claude 结构化提取 → AI 分析日报，并支持通过自然语言对话完成 Paper Trading（买卖/查仓）。单用户本地部署，不涉及真实资金。

---

## 常用命令

所有命令在 `stock_insight_agent/` 目录下执行：

```bash
python manage.py runserver                  # 启动开发服务器
python manage.py run_pipeline               # 完整 pipeline（采集 → 提取 → 报告）
python manage.py makemigrations && python manage.py migrate
tail -f logs/pipeline.log                   # 实时日志
python manage.py shell                      # Django shell 调试
```

---

## 目录结构与模块职责

```
stock_insight_agent/
├── config/settings.py          # Django 配置，API Keys 全部从环境变量读取
├── apps/pipeline/
│   ├── models.py               # NewsArticle, DailyReport
│   ├── collector.py            # Module 1：三路新闻采集（NewsAPI / Alpha Vantage / Yahoo RSS）
│   ├── extractor.py            # Module 2：Claude 批量结构化提取（每批 10 条）
│   ├── reporter.py             # Module 3：统计汇总 + 7 步 AI 报告生成
│   └── management/commands/run_pipeline.py
└── apps/api/
    ├── views.py                # 6 个 HTTP endpoint + chat agent 逻辑
    └── templates/index.html    # 前端单文件（Vanilla JS + Chart.js CDN）
```

**模块边界（严格执行）：**
- `collector.py` — 只采集写 DB，不做提取
- `extractor.py` — 只更新 `NewsArticle` 提取字段，不做报告
- `reporter.py` — 只汇总统计和生成报告，不做采集
- `views.py` — 只做 HTTP 请求/响应，不写 pipeline 逻辑

---

## 关键实现细节（非显而易见，改动前必读）

### Collector
- 若当天已有 ≥20 条新闻则跳过采集（除非传 `force=True`）
- 用 `get_or_create(url=...)` 做幂等，URL 是唯一键
- `_collect_running` 全局 flag 防止并发采集（后台线程）

### Extractor
- 使用模型：`claude-sonnet-4-6`
- 批量处理：每批 10 条，串行调用；JSON 解析失败重试 1 次；仍失败用 `default_extraction()` 填充
- `sentiment` 判断的是**股价影响**（非情绪色彩）：如"监管调查苹果" → Bearish
- `impact_level=High` 表示预期涨跌 >2%

### Reporter（7 步 AI 流程）
1. `compute_summary()` — Python 统计（情绪分布、事件类型、top tickers）
2. `fetch_portfolio()` — Alpaca REST API 查持仓
3. `plan()` — **Sonnet** 生成报告大纲
4. `execute_report()` — **Haiku** 写完整草稿（5 个区块）
5+6. `reflect_and_replan()` — **Haiku** 批评 + 改进方向（合并为 1 次调用）
7. `final_report()` — **Haiku** 输出最终版；若为空则回退草稿区块

报告区块用分隔符解析，顺序固定：
```
===AI_HOTSPOTS===  ===EVENT_ANALYSIS===  ===TREND_INSIGHTS===
===RISK_OPPORTUNITY===  ===PORTFOLIO_INSIGHT===
```

使用模型：`claude-haiku-4-5-20251001`（步骤 4-7）、`claude-sonnet-4-6`（步骤 3）

### Chat Agent
- **工具列表**（`views.py` 中定义，改名/增删需同步前端）：
  `get_positions` / `get_account` / `get_stock_snapshot` / `place_order` / `close_position` / `get_price_chart`
- agentic loop 最多 5 次工具调用；保留最近 10 轮对话历史
- Chat 响应 JSON 结构：`{reply, chart_data, requires_confirmation, action_type}`
- 图表数据通过 `__chart__` 标记注入响应，前端识别后用 Chart.js 渲染折线图
- **安全规则**：必须用户输入"确认/是/yes/confirm"后才执行交易；仅 Paper Trading

### 前端
- 聊天历史存 localStorage，key = `CHAT_STORAGE_KEY = 'ai_insight_chat_v1'`
- 股价图表：股票用 Alpha Vantage，加密货币用 Alpaca（`_is_crypto()` 判断）
- `DailyReport.report_data` 结构：`{date, summary, top_tickers, highlights, analysis_text}`
  - `analysis_text` 含 5 个区块 key（对应分隔符名称小写）

---

## 编码规范

- Python snake_case；Model 字段 snake_case；URL name snake_case；常量 UPPER_SNAKE_CASE
- 日志用 `logger.error/warning/info()`，**禁止用 `print()`**，写入 `logs/pipeline.log`
- 时区统一 `America/New_York`（settings 已配置），ORM 查询日期注意时区转换
- **禁止** hardcode API Key；**禁止** 新建第三方库（需先讨论确认）；**禁止** 新建 Django app

---

## MVP 边界

**已有：** 单用户本地部署 / Paper Trading / 6 API endpoint / Chat Agent / 每日 pipeline

**无：** 用户认证 / WebSocket / 回测 / 多用户 / 真实资金交易

**IMPORTANT：任何超出 MVP 范围的功能，必须先询问用户确认，不得自行添加。**

---

## 工作约定

1. **实现新功能前：** 先说明理解和实现计划，等用户确认再动手
2. **实现完成后：** 告知用户具体验证步骤（命令或操作）
3. **代码与文档冲突时：** 以代码为准，告知用户并建议更新文档

---

## 功能验证清单

每次实现或修改功能后，按对应模块运行验证：

### Module 1 — 采集（collector.py）
```bash
python manage.py run_pipeline
tail -f logs/pipeline.log          # 确认无 ERROR

python manage.py shell
>>> from apps.pipeline.models import NewsArticle
>>> NewsArticle.objects.filter(is_extracted=False).count()        # 应 > 0（待提取）
>>> NewsArticle.objects.order_by('-fetched_at')[:3].values('title', 'source_type')
```

### Module 2 — 提取（extractor.py）
```bash
python manage.py shell
>>> from apps.pipeline.models import NewsArticle
>>> NewsArticle.objects.filter(is_extracted=True).count()                          # 应与采集数匹配
>>> NewsArticle.objects.filter(one_line_summary='[Extraction failed]').count()     # 应为 0
```

### Module 3 — 报告（reporter.py）
```bash
python manage.py shell
>>> from apps.pipeline.models import DailyReport
>>> r = DailyReport.objects.latest('report_date')
>>> r.status                              # 应为 'completed'
>>> list(r.report_data.keys())            # 应含 date / summary / top_tickers / highlights / analysis_text
>>> r.report_data['summary']['total_news_count']   # 应 > 0
```

### API Endpoints
```bash
curl http://localhost:8000/api/news/           # 返回新闻列表 JSON
curl http://localhost:8000/api/portfolio/      # 返回持仓数据 JSON
curl http://localhost:8000/api/report/latest/  # 返回最新报告 JSON
curl -X POST http://localhost:8000/api/collect/
curl -X POST http://localhost:8000/api/report/generate/
curl -X POST http://localhost:8000/api/chat/ \
  -H "Content-Type: application/json" \
  -d '{"message": "查看我的仓位"}'
```

### 前端页面
```
http://localhost:8000
1. 新闻列表、报告内容、持仓卡片、Chat 对话框 均正常渲染
2. "手动触发新闻收集" → 应触发采集并刷新列表
3. "生成今日分析报告" → 显示加载状态，完成后刷新报告区域
4. Chat 输入"查看我的仓位" → 返回持仓信息
5. 浏览器 Console 无报错
```

### 端到端
```bash
python manage.py run_pipeline
tail -20 logs/pipeline.log
# 期望末尾出现：Collected X | Extracted X | Report generated for YYYY-MM-DD
```

---

## API Endpoints 速查

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/` | GET | 前端页面 |
| `/api/news/` | GET | 新闻列表（支持 impact_level / sentiment / source_type 过滤） |
| `/api/portfolio/` | GET | Alpaca 持仓 |
| `/api/collect/` | POST | 触发采集+提取（后台线程） |
| `/api/report/generate/` | POST | 触发报告生成 |
| `/api/report/latest/` | GET | 获取最新报告 |
| `/api/chat/` | POST | `{"message": "..."}` → `{reply, chart_data, ...}` |
