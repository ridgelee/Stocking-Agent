"""
reporter.py — 报告生成层（Planner-Executor-Reflection-Replan-Final 架构）

生成流程（5步 AI 调用）：
  Step A: compute_summary()     — 纯 Python 统计，无 AI 调用
  Step B: fetch_portfolio()     — 直接调用 Alpaca REST API 获取持仓
  Step C: plan()                — AI Planner：对今日数据进行规划，确定报告重点
  Step D: execute_report()      — AI Executor：按 plan 生成完整报告草稿
  Step E: reflect()             — AI Reflection：对草稿进行批判性评估
  Step F: replan()              — AI Replan：根据 reflection 制定改进方向
  Step G: final_report()        — AI Final：根据 replan 生成最终报告
  Step H: 存入 DailyReport 表
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
    counter = Counter()
    for article in articles:
        tickers = article.related_tickers or []
        if isinstance(tickers, list):
            counter.update(tickers)
    return [{"ticker": t, "count": c} for t, c in counter.most_common()]


def select_top_highlights(articles, limit=8):
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
            "source_type": a.source_type,
        }
        for a in selected
    ]


def compute_summary(articles):
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
        "highlights": select_top_highlights(articles, limit=8),
    }


# ---------------------------------------------------------------------------
# Step B — Alpaca REST API 获取持仓
# ---------------------------------------------------------------------------

def fetch_portfolio():
    from alpaca.trading.client import TradingClient

    api_key = settings.ALPACA_API_KEY
    secret_key = settings.ALPACA_SECRET_KEY
    if not api_key or not secret_key:
        logger.warning("fetch_portfolio: ALPACA keys not set, returning []")
        return []
    try:
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        raw = client.get_all_positions()
        positions = [
            {
                "symbol": str(p.symbol),
                "qty": str(p.qty),
                "avg_entry_price": str(p.avg_entry_price),
                "current_price": str(p.current_price),
                "market_value": str(p.market_value),
                "unrealized_pl": str(p.unrealized_pl),
                "unrealized_plpc": str(p.unrealized_plpc),
            }
            for p in raw
        ]
        logger.info(f"fetch_portfolio: 获取 {len(positions)} 条持仓")
        return positions
    except Exception as e:
        logger.warning(f"fetch_portfolio: 失败 ({type(e).__name__}: {e})，使用 fallback []")
        return []


# ---------------------------------------------------------------------------
# Helper — 构建数据 context 字符串（各步骤共用）
# ---------------------------------------------------------------------------

def _build_context(summary: dict, portfolio: list, report_date: str) -> str:
    highlights_text = "\n".join(
        f"- [{h.get('impact_level','?')}][{h.get('sentiment','?')}] {h.get('summary', h.get('title',''))}"
        for h in summary.get("highlights", [])[:5]  # top 5 only
    )
    top_tickers_text = ", ".join(
        f"{t['ticker']}({t['count']})" for t in summary.get("top_tickers", [])[:8]
    )
    # Compact portfolio: just symbol, current_price, unrealized_plpc
    portfolio_compact = [
        {"s": p.get("symbol"), "price": p.get("current_price"), "pnl%": p.get("unrealized_plpc")}
        for p in portfolio
    ] if portfolio else []
    portfolio_text = json.dumps(portfolio_compact, ensure_ascii=False) if portfolio_compact else "暂无持仓"
    sent = summary['sentiment_distribution']
    return f"""日期:{report_date} 新闻:{summary['total_news_count']}条(高影响:{summary['high_impact_count']}) 情绪:看多{sent.get('Bullish',0)}/看空{sent.get('Bearish',0)}/中性{sent.get('Neutral',0)}
热门股:{top_tickers_text}
精选新闻(top5):
{highlights_text}
持仓:{portfolio_text}"""


SECTION_KEYS = ["AI_HOTSPOTS", "EVENT_ANALYSIS", "TREND_INSIGHTS", "RISK_OPPORTUNITY", "PORTFOLIO_INSIGHT"]
SECTION_MAP = {
    "AI_HOTSPOTS": "ai_hotspots",
    "EVENT_ANALYSIS": "event_analysis",
    "TREND_INSIGHTS": "trend_insights",
    "RISK_OPPORTUNITY": "risk_opportunity",
    "PORTFOLIO_INSIGHT": "portfolio_insight",
}


def _parse_sections(raw: str) -> dict:
    """Parse delimiter-based section format into a dict."""
    result = {}
    current_key = None
    current_lines = []
    for line in raw.split("\n"):
        stripped = line.strip()
        matched = None
        for k in SECTION_KEYS:
            if stripped == f"==={k}===":
                matched = k
                break
        if matched:
            if current_key and current_lines:
                result[SECTION_MAP[current_key]] = "\n".join(current_lines).strip()
            current_key = matched
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)
    if current_key and current_lines:
        result[SECTION_MAP[current_key]] = "\n".join(current_lines).strip()
    return result


def _call_claude(system: str, user: str, max_tokens: int = 2048, model: str = "claude-sonnet-4-6") -> str:
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()


HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

_CRYPTO_BASES = {"BTC", "ETH", "SOL", "DOGE", "AVAX", "LTC", "BCH", "LINK", "UNI", "AAVE", "USDC", "USDT"}


def _strip_markdown(text: str) -> str:
    """Remove common markdown formatting from text."""
    import re
    # Remove bold/italic markers
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # Remove heading markers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Remove bullet point markers (-, *, ·) at line start but keep content
    text = re.sub(r'^\s*[-*·]\s+', '• ', text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _classify_portfolio_sectors(portfolio: list) -> dict:
    """Group holdings by sector. Returns {sector_name: [symbol, ...]}."""
    sectors = {}
    for p in portfolio:
        sym = (p.get("symbol") or "").upper().replace("/", "")
        base = next((c for c in sorted(_CRYPTO_BASES, key=len, reverse=True) if sym.startswith(c)), None)
        if base:
            sector = "加密货币"
        elif sym.endswith("USD") and len(sym) > 3:
            sector = "加密货币"
        else:
            sector = "科技股/其他"
        sectors.setdefault(sector, []).append(sym)
    return sectors


def _parse_json(raw: str) -> dict:
    # Strip markdown fences
    if "```" in raw:
        lines = raw.split("\n")
        # Remove first and last fence lines
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines)
    raw = raw.strip()
    # Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting outermost { ... }
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        candidate = raw[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # Last resort: use Claude to fix the broken JSON
    logger.warning("_parse_json: standard parse failed, attempting repair")
    try:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        repair_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system="Fix the broken JSON below and return ONLY valid JSON, nothing else.",
            messages=[{"role": "user", "content": raw[:8000]}],
        )
        repaired = repair_resp.content[0].text.strip()
        if "```" in repaired:
            repaired = "\n".join([l for l in repaired.split("\n") if not l.strip().startswith("```")])
        return json.loads(repaired)
    except Exception as e:
        raise ValueError(f"JSON parse failed after repair attempt: {e}") from e


# ---------------------------------------------------------------------------
# Step C — AI Planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """You are a senior AI & technology market analyst. Given today's news data, produce a brief report plan in Chinese using EXACTLY this format:

TOP_EVENTS: event1 | event2 | event3
KEY_TICKERS: TICK1, TICK2, TICK3
FOCUS_THEMES: theme1 | theme2
RISK: main risk signal
OPPORTUNITY: main opportunity
PORTFOLIO_NOTE: one line

Plain text only, no JSON, no markdown."""


def _parse_plan_text(raw: str) -> dict:
    """Parse plain-text plan format into dict."""
    result = {"top_events": [], "key_tickers": [], "focus_themes": [], "risk_signals": [], "opportunity_signals": [], "portfolio_relevance": ""}
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("TOP_EVENTS:"):
            result["top_events"] = [e.strip() for e in line[11:].split("|") if e.strip()]
        elif line.startswith("KEY_TICKERS:"):
            result["key_tickers"] = [t.strip() for t in line[12:].split(",") if t.strip()]
        elif line.startswith("FOCUS_THEMES:"):
            result["focus_themes"] = [t.strip() for t in line[13:].split("|") if t.strip()]
        elif line.startswith("RISK:"):
            result["risk_signals"] = [line[5:].strip()]
        elif line.startswith("OPPORTUNITY:"):
            result["opportunity_signals"] = [line[12:].strip()]
        elif line.startswith("PORTFOLIO_NOTE:"):
            result["portfolio_relevance"] = line[15:].strip()
    return result


def plan(summary: dict, portfolio: list) -> dict:
    logger.info("[reporter] Step C: Planner 规划报告重点")
    highlights = [h.get("summary", h.get("title", ""))[:80] for h in summary.get("highlights", [])[:5]]
    tickers = [t["ticker"] for t in summary.get("top_tickers", [])[:6]]
    holdings = [p.get("symbol") for p in portfolio]
    brief = f"""新闻{summary['total_news_count']}条,高影响{summary['high_impact_count']},情绪{summary['sentiment_distribution']}
热门:{tickers} 持仓:{holdings}
头条:{highlights}"""
    raw = _call_claude(PLANNER_SYSTEM, f"{brief}\n\n请制定今日报告规划。", max_tokens=300)
    result = _parse_plan_text(raw)
    logger.info(f"[reporter] Planner 完成，top_events={result.get('top_events', [])[:1]}")
    return result


# ---------------------------------------------------------------------------
# Step D — AI Executor（按 plan 生成报告草稿）
# ---------------------------------------------------------------------------

EXECUTOR_SYSTEM = """You are a senior AI & technology market analyst. Generate a detailed daily report in Chinese following the plan provided.

Use EXACTLY this format with these delimiter lines (output the delimiters literally):

===AI_HOTSPOTS===
[Top 3-5 AI/tech events, each with specific numbers]

===PORTFOLIO_INSIGHT===
[Only cover the sectors that are actually held (provided in the data). Per held sector: 1-2 sentences on today's news relevance, then one clear recommendation (持有/加仓/减仓/观望) with brief rationale. No per-stock breakdown.]
⚠️ 以上为模拟交易参考建议，不构成真实投资建议，投资有风险。

===EVENT_ANALYSIS===
[Background + market impact per event, data-backed]

===TREND_INSIGHTS===
[Directional insights: tech / policy / capital]

===RISK_OPPORTUNITY===
[【风险】and 【机会】clearly separated]"""


def execute_report(context: str, report_plan: dict) -> dict:
    logger.info("[reporter] Step D: Executor 生成报告草稿")
    user = f"""今日数据：
{context}

报告规划：
{json.dumps(report_plan, ensure_ascii=False, indent=2)}

请按规划生成完整报告草稿。"""
    raw = _call_claude(EXECUTOR_SYSTEM, user, max_tokens=2500, model=HAIKU)
    result = _parse_sections(raw)
    if not result.get("ai_hotspots"):
        logger.warning("[reporter] Executor: section parse found no content, raw preview: " + raw[:200])
    logger.info("[reporter] Executor 草稿生成完成")
    return result


# ---------------------------------------------------------------------------
# Step E+F — AI Reflect + Replan（合并为单次 Haiku 调用）
# ---------------------------------------------------------------------------

REFLECT_REPLAN_SYSTEM = """You are a report editor. Critique the draft and give improvement instructions in Chinese using EXACTLY this format:

SCORE: 7
FIX1: most important fix
FIX2: second fix
FIX3: third fix
KEY_FIX: single most critical improvement

Plain text only, no JSON, no markdown."""


def reflect_and_replan(draft: dict, context: str, report_plan: dict) -> dict:
    logger.info("[reporter] Step E+F: Reflect+Replan")
    draft_summary = " | ".join(
        f"{k}:{str(v)[:120]}" for k, v in draft.items() if v
    )
    user = f"草稿:{draft_summary}\n\n请评估并给出改进方案。"
    raw = _call_claude(REFLECT_REPLAN_SYSTEM, user, max_tokens=250, model=HAIKU)
    # Parse plain text format
    result = {"overall_score": None, "improvement_priorities": [], "key_fix": "", "sections_to_strengthen": {}}
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                result["overall_score"] = int(line[6:].strip())
            except ValueError:
                pass
        elif line.startswith("FIX"):
            colon = line.find(":")
            if colon != -1:
                result["improvement_priorities"].append(line[colon+1:].strip())
        elif line.startswith("KEY_FIX:"):
            result["key_fix"] = line[8:].strip()
    logger.info(f"[reporter] Reflect+Replan 完成，score={result.get('overall_score')}, key_fix={result.get('key_fix','')[:60]}")
    return result


# ---------------------------------------------------------------------------
# Step G — AI Final Report（最终版本）
# ---------------------------------------------------------------------------

FINAL_SYSTEM = """You are a senior AI & technology market analyst producing the FINAL polished daily report in Chinese.
Apply ALL improvements from the replan. Make it sharp, specific, complete.

Use EXACTLY this format with these delimiter lines (output the delimiters literally):

===AI_HOTSPOTS===
[Top 3-5 AI/tech events with specific numbers/metrics]

===EVENT_ANALYSIS===
[Background + impact per event, always data-backed]

===TREND_INSIGHTS===
[Concrete directional insights: tech / policy / capital]

===RISK_OPPORTUNITY===
[【风险】section and 【机会】section clearly separated, actionable]

===PORTFOLIO_INSIGHT===
[Group holdings by sector (e.g. 加密货币、科技股、卫星通信). For each sector: summarize current exposure, relate to today's news themes, give one actionable sector-level recommendation (持有/加仓/减仓/观望) with rationale]
⚠️ 以上为模拟交易参考建议，不构成真实投资建议，投资有风险。"""


def final_report(context: str, draft: dict, reflect_replan_result: dict, portfolio: list = None) -> dict:
    logger.info("[reporter] Step G: Final Report 生成最终版本")
    draft_summary = "\n".join(
        f"[{k.upper()}]\n{str(v)[:300]}..." if len(str(v)) > 300 else f"[{k.upper()}]\n{v}"
        for k, v in draft.items() if v
    )
    portfolio_text = ""
    if portfolio:
        sectors = _classify_portfolio_sectors(portfolio)
        sector_summary = "、".join(
            f"{s}({', '.join(syms)})" for s, syms in sectors.items()
        )
        portfolio_text = f"\n当前持仓板块：{sector_summary}"

    user = f"""今日数据：
{context}{portfolio_text}

报告草稿：
{draft_summary}

改进重点：{json.dumps(reflect_replan_result.get('improvement_priorities', []), ensure_ascii=False)}
关键修复：{reflect_replan_result.get('key_fix', '')}

请生成最终版今日分析报告。PORTFOLIO_INSIGHT章节只针对上面列出的持仓板块给建议，不要分析未持有的板块。"""
    raw = _call_claude(FINAL_SYSTEM, user, max_tokens=2500, model=HAIKU)
    result = _parse_sections(raw)

    # Fall back to draft section if final is empty
    for key in SECTION_MAP.values():
        if not result.get(key) and draft.get(key):
            result[key] = draft[key]

    # Strip markdown from all sections
    result = {k: _strip_markdown(v) if isinstance(v, str) else v for k, v in result.items()}

    # Ensure disclaimer
    disclaimer = "⚠️ 以上为模拟交易参考建议，不构成真实投资建议，投资有风险。"
    if "⚠️" not in result.get("portfolio_insight", ""):
        result["portfolio_insight"] = result.get("portfolio_insight", "") + f"\n\n{disclaimer}"

    logger.info("[reporter] Final Report 生成完成")
    return result


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run(date=None):
    """
    Planner-Executor-Reflect+Replan-Final 四步 AI 报告生成流程：
      A: compute_summary  — Python 统计
      B: fetch_portfolio  — Alpaca REST
      C: plan()           — Sonnet Planner（规划报告重点）
      D: execute_report() — Haiku Executor（生成草稿）
      E+F: reflect_and_replan() — Haiku（评估+改进，合并单次调用）
      G: final_report()   — Haiku Final（生成最终版）
      H: save to DailyReport
    """
    if date is None:
        date = date_type.today()

    logger.info(f"[reporter] 开始生成 {date} 报告（Planner-Executor-Reflection-Replan-Final）")

    # Step A
    from django.utils import timezone as dj_tz
    today = dj_tz.now().date()
    articles = list(
        NewsArticle.objects.filter(is_extracted=True, fetched_at__date=today)
        .order_by("-impact_level", "-published_at")[:20]
    )
    logger.info(f"[reporter] Step A: {len(articles)} 条已提取新闻")
    summary = compute_summary(articles)

    # Step B
    logger.info("[reporter] Step B: 获取 Alpaca 持仓")
    portfolio = fetch_portfolio()

    context = _build_context(summary, portfolio, str(date))

    # Step C: Plan
    try:
        report_plan = plan(summary, portfolio)
    except Exception as e:
        logger.warning(f"[reporter] Planner 失败: {e}，使用空 plan")
        report_plan = {}

    # Step D: Execute
    try:
        draft = execute_report(context, report_plan)
    except Exception as e:
        logger.warning(f"[reporter] Executor 失败: {e}，使用降级内容")
        draft = {
            "ai_hotspots": "（AI 分析暂不可用）",
            "event_analysis": "（AI 分析暂不可用）",
            "trend_insights": "（AI 分析暂不可用）",
            "risk_opportunity": "（AI 分析暂不可用）",
            "portfolio_insight": "（AI 分析暂不可用）\n\n⚠️ 以上为模拟交易参考建议，不构成真实投资建议，投资有风险。",
        }

    # Step E+F: Reflect + Replan (merged, Haiku)
    try:
        ef_result = reflect_and_replan(draft, context, report_plan)
    except Exception as e:
        logger.warning(f"[reporter] Reflect+Replan 失败: {e}，跳过")
        ef_result = {"overall_score": None, "improvement_priorities": [], "sections_to_strengthen": {}, "key_fix": ""}

    # Step G: Final (Haiku)
    try:
        analysis_text = final_report(context, draft, ef_result, portfolio=portfolio)
    except Exception as e:
        logger.warning(f"[reporter] Final Report 失败: {e}，使用草稿")
        analysis_text = draft

    # Assemble full report
    full_report_data = {
        "report_date": str(date),
        **summary,
        "portfolio": portfolio,
        "analysis_text": analysis_text,
        "generation_meta": {
            "method": "planner(sonnet)→executor(haiku)→reflect+replan(haiku)→final(haiku)",
            "reflection_score": ef_result.get("overall_score"),
            "plan_top_events": report_plan.get("top_events", []),
        },
    }

    # Step H: Save
    obj, created = DailyReport.objects.update_or_create(
        report_date=date,
        defaults={"report_data": full_report_data, "status": "completed"},
    )
    action = "创建" if created else "更新"
    logger.info(f"[reporter] 报告已{action}，id={obj.pk}")
    print(f"[reporter] 报告已{action}，report_date={date}, status=completed")
    return obj
