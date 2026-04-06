"""
reporter.py — 报告生成层

四步生成流程：
  Step A: compute_summary()  — 纯 Python 统计，无 AI 调用
  Step B: fetch_portfolio()  — 通过 Anthropic SDK mcp_servers 调用 Alpaca MCP 获取持仓
  Step C: generate_analysis() — 1次 Claude API 调用，生成三章节分析文本
  Step D: 存入 DailyReport 表（update_or_create）
"""

import json
import logging
from collections import Counter
from datetime import date as date_type

import anthropic
from django.conf import settings

from .models import DailyReport, NewsArticle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step A — 纯 Python 统计
# ---------------------------------------------------------------------------

def compute_ticker_frequency(articles):
    """统计所有 related_tickers 出现频次，返回排序后的列表。"""
    counter = Counter()
    for article in articles:
        tickers = article.related_tickers or []
        if isinstance(tickers, list):
            counter.update(tickers)
    return [{"ticker": t, "count": c} for t, c in counter.most_common()]


def select_top_highlights(articles, limit=5):
    """
    优先级：impact_level=High > Bullish/Bearish > Neutral
    返回最多 limit 条文章的 one_line_summary（或 title）。
    """
    high = [a for a in articles if a.impact_level == "High"]
    mid = [a for a in articles if a.impact_level != "High" and a.sentiment in ("Bullish", "Bearish")]
    low = [a for a in articles if a.impact_level != "High" and a.sentiment not in ("Bullish", "Bearish")]

    selected = (high + mid + low)[:limit]
    return [
        {
            "title": a.title,
            "summary": a.one_line_summary or a.title,
            "impact_level": a.impact_level,
            "sentiment": a.sentiment,
            "event_type": a.event_type,
        }
        for a in selected
    ]


def compute_summary(articles):
    """Step A: 统计汇总，纯 Python，无 AI 调用。"""
    sentiment_dist = {"Bullish": 0, "Bearish": 0, "Neutral": 0}
    event_type_dist = {}

    for a in articles:
        if a.sentiment in sentiment_dist:
            sentiment_dist[a.sentiment] += 1
        if a.event_type:
            event_type_dist[a.event_type] = event_type_dist.get(a.event_type, 0) + 1

    return {
        "total_news_count": len(articles),
        "high_impact_count": sum(1 for a in articles if a.impact_level == "High"),
        "sentiment_distribution": sentiment_dist,
        "event_type_distribution": event_type_dist,
        "top_tickers": compute_ticker_frequency(articles)[:10],
        "highlights": select_top_highlights(articles, limit=5),
    }


# ---------------------------------------------------------------------------
# Step B — Alpaca MCP via Anthropic SDK mcp_servers
# ---------------------------------------------------------------------------

def fetch_portfolio():
    """
    直接调用 Alpaca REST API /v2/positions 获取持仓。
    失败时返回 []，记录日志，不中断报告生成。
    返回格式：
    [{"symbol": "NVDA", "qty": "10", "avg_entry_price": "800.00",
      "current_price": "875.00", "market_value": "8750.00",
      "unrealized_pl": "750.00", "unrealized_plpc": "0.094"}]
    """
    import requests as req

    base_url = getattr(settings, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    api_key = settings.ALPACA_API_KEY
    secret_key = settings.ALPACA_SECRET_KEY

    if not api_key or not secret_key:
        logger.warning("fetch_portfolio: ALPACA keys not set, returning []")
        return []

    try:
        resp = req.get(
            f"{base_url}/v2/positions",
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        positions = [
            {
                "symbol": p.get("symbol"),
                "qty": p.get("qty"),
                "avg_entry_price": p.get("avg_entry_price"),
                "current_price": p.get("current_price"),
                "market_value": p.get("market_value"),
                "unrealized_pl": p.get("unrealized_pl"),
                "unrealized_plpc": p.get("unrealized_plpc"),
            }
            for p in raw
        ]
        logger.info(f"fetch_portfolio: 成功获取 {len(positions)} 条持仓")
        return positions
    except Exception as e:
        logger.warning(f"fetch_portfolio: 调用失败 ({type(e).__name__}: {e})，使用 fallback []")
        return []


# ---------------------------------------------------------------------------
# Step C — Claude API 分析（1次调用）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior equity market analyst. Generate a structured daily market report in Chinese.

Your report MUST contain exactly these three sections:
## 今日市场趋势
[2-3 sentences describing today's dominant market themes based on the statistics]

## 重要事件深度总结
[Top 3-5 important events. For EACH event: background + market impact, 2-3 sentences each]
REQUIREMENT: Must include specific data/numbers.
FORBIDDEN: Vague statements like "影响较大" or "值得关注" without data support.

## 持仓相关投资参考
[For each holding in the portfolio: explain relevant news impact]
[If no relevant news: write "今日暂无直接相关新闻"]
MANDATORY LAST LINE: ⚠️ 以上内容仅供参考，不构成投资建议。

Return the three sections as a JSON object with keys: market_trend, event_summary, portfolio_insight
JSON only, no markdown fences."""


def generate_analysis(summary: dict, portfolio: list, report_date: str) -> dict:
    """
    Step C: 调用 Claude API 一次，生成三章节分析文本。
    失败时返回降级文本，不 crash。
    """
    fallback = {
        "market_trend": "（AI 分析暂不可用）今日市场数据已采集，请稍后重试。",
        "event_summary": "（AI 分析暂不可用）重要事件汇总生成失败，请稍后重试。",
        "portfolio_insight": "（AI 分析暂不可用）持仓分析生成失败。\n\n⚠️ 以上内容仅供参考，不构成投资建议。",
    }

    highlights_text = "\n".join(
        f"- [{h.get('impact_level','?')}][{h.get('sentiment','?')}] {h.get('summary', h.get('title',''))}"
        for h in summary.get("highlights", [])
    )
    top_tickers_text = ", ".join(
        f"{t['ticker']}({t['count']})" for t in summary.get("top_tickers", [])[:10]
    )
    portfolio_text = (
        json.dumps(portfolio, ensure_ascii=False, indent=2) if portfolio else "暂无持仓"
    )

    user_message = f"""
报告日期：{report_date}
总新闻数：{summary['total_news_count']}
高影响新闻数：{summary['high_impact_count']}
情绪分布：{json.dumps(summary['sentiment_distribution'], ensure_ascii=False)}
事件类型分布：{json.dumps(summary['event_type_distribution'], ensure_ascii=False)}
高频股票代码（top10）：{top_tickers_text}

精选新闻亮点（最多5条）：
{highlights_text}

当前持仓：
{portfolio_text}

请根据以上数据生成今日市场报告。
""".strip()

    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])  # drop first ```json line
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3].strip()

        # Try direct parse first
        try:
            analysis = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: extract JSON object using brace matching
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                analysis = json.loads(raw[start:end + 1])
            else:
                raise

        # Ensure disclaimer in portfolio_insight
        if "⚠️" not in analysis.get("portfolio_insight", ""):
            analysis["portfolio_insight"] = (
                analysis.get("portfolio_insight", "") + "\n\n⚠️ 以上内容仅供参考，不构成投资建议。"
            )

        logger.info("generate_analysis: Claude 分析生成成功")
        return analysis

    except Exception as e:
        logger.warning(f"generate_analysis: 失败 ({type(e).__name__}: {e})，使用降级文本")
        return fallback


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(date=None):
    """
    Step A: compute_summary
    Step B: fetch_portfolio
    Step C: generate_analysis (1次 Claude API)
    Step D: save to DailyReport
    """
    if date is None:
        date = date_type.today()

    logger.info(f"[reporter] 开始生成 {date} 报告")

    # Step A
    articles = list(NewsArticle.objects.filter(is_extracted=True))
    logger.info(f"[reporter] Step A: 共 {len(articles)} 条已提取新闻")
    summary = compute_summary(articles)

    # Step B
    logger.info("[reporter] Step B: 获取持仓")
    portfolio = fetch_portfolio()
    logger.info(f"[reporter] Step B: 持仓条数 = {len(portfolio)}")

    # Step C
    logger.info("[reporter] Step C: 调用 Claude 生成分析")
    analysis_text = generate_analysis(summary, portfolio, str(date))

    # Assemble full report
    full_report_data = {
        "report_date": str(date),
        **summary,
        "portfolio": portfolio,
        "analysis_text": analysis_text,
    }

    # Step D
    obj, created = DailyReport.objects.update_or_create(
        report_date=date,
        defaults={"report_data": full_report_data, "status": "completed"},
    )
    action = "创建" if created else "更新"
    logger.info(f"[reporter] Step D: 报告已{action}，id={obj.pk}")
    print(f"[reporter] 报告已{action}，report_date={date}, status=completed")
    return obj
