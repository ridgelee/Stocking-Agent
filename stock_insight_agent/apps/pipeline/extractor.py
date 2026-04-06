"""
apps/pipeline/extractor.py

结构化提取模块：调用 Claude API 对新闻进行结构化信息提取。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Schema 设计思路（核心设计原则：结构化抽取，不是摘要）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. related_tickers（JSON Array，max 5）
   为什么：不同来源对同一公司指代方式不同（"Nvidia" / "NVDA" / "the chip maker"）。
   统一成 ticker symbol 后，可直接按 ticker 聚合，计算每只股票的舆情热度频次。
   为什么用数组：一篇新闻可能涉及多只股票（如"苹果收购 Beats"→ [AAPL]）。

2. event_type（Enum: 6类）
   为什么用枚举而不是自由文本：投资决策中不同事件权重不同，需要可统计。
   财报 > 并购 > 政策 > 产品发布 > 市场波动 > 其他。
   枚举保证后端可直接做分组统计（今日哪类事件最多）。

3. sentiment（Bullish / Bearish / Neutral）
   关键：情绪是对"此新闻对股价的预期影响"的判断，不是新闻语气。
   例：一篇中立语气的"监管机构调查苹果"= Bearish（因为预期会压股价）。
   这种语义区分是 Claude 相比关键词情感分析的核心价值。

4. impact_level（High / Medium / Low）
   High = 可能引发 >2% 股价波动（财报超预期/重大并购/政策冲击）。
   用于前端优先展示高影响事件，帮助投资者快速定位重要信息。

5. one_line_summary（Text, max 50 words）
   不是原文摘要，而是"市场含义摘要"：这件事对投资者意味着什么？
   必须比原标题包含更多市场解读（数据支撑 + 潜在影响方向）。

6. key_entities（JSON Array，max 5）
   提取人名/公司名/产品名，用于跨新闻关联分析（同一公司多条新闻聚合）。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
import time
from typing import Optional

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

SYSTEM_PROMPT = """You are a financial news analyst specializing in equity markets.
Extract structured information from news articles for stock market analysis.

STRICT RULES:
- Respond with a valid JSON array ONLY. No markdown, no explanation, no code fences.
- The array must have exactly the same number of elements as the input articles.
- related_tickers: stock ticker symbols only (e.g. NVDA, not "Nvidia"). Max 5. Use [] if none.
- event_type: MUST be exactly one of: earnings, merger_acquisition, policy, product_launch, market_movement, other
- sentiment: MUST be exactly one of: Bullish, Bearish, Neutral
  Judge by likely stock PRICE impact, NOT article tone.
  Example: "Regulator investigates Apple" → Bearish (even if tone is neutral)
- impact_level: MUST be exactly one of: High, Medium, Low
  High = likely >2% price move (earnings beat/miss, major M&A, major policy)
  Medium = moderate market reaction expected
  Low = minor or informational only
- one_line_summary: max 50 words, explain MARKET IMPLICATIONS not just facts.
  Must include data/numbers if available. Bad: "This may impact markets." Good: "NVIDIA Q4 revenue beat by 20%, driven by H100 demand; likely to push AI infra spending higher and pressure AMD."
- key_entities: company names, people, products only. Max 5. Use [] if none."""


def default_extraction() -> dict:
    """提取失败时的降级默认值"""
    return {
        "related_tickers": [],
        "event_type": "other",
        "sentiment": "Neutral",
        "impact_level": "Low",
        "one_line_summary": "[Extraction failed - using defaults]",
        "key_entities": [],
    }


def extract_batch(articles: list) -> list:
    """
    批量提取，articles 为 NewsArticle 对象列表（最多 BATCH_SIZE 条）。
    返回与输入等长的提取结果列表。

    错误处理策略：
    1. JSONDecodeError → 用更强语气重试一次
    2. 第二次失败 → 全批次填充 default_extraction()，记录日志
    3. Claude API 超时/网络错误 → 等待 5s 重试，最多 2 次
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    # 构造 user prompt
    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += f"""
Article {i}:
Title: {article.title}
Source: {article.source_name}
Content: {(article.content or '')[:800]}
---"""

    user_prompt = f"""Extract structured information from the following {len(articles)} news articles.
Return a JSON array with exactly {len(articles)} objects, one per article, in the same order.

{articles_text}

Return ONLY the JSON array. Example format:
[{{"related_tickers": ["NVDA"], "event_type": "earnings", "sentiment": "Bullish", "impact_level": "High", "one_line_summary": "...", "key_entities": ["NVIDIA", "Jensen Huang"]}}]"""

    def call_claude(prompt: str, retry_msg: str = "") -> Optional[list]:
        """调用 Claude API，返回解析后的列表，失败返回 None"""
        for attempt in range(2):
            try:
                full_prompt = prompt if not retry_msg else prompt + f"\n\n{retry_msg}"
                response = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": full_prompt}],
                )
                raw = response.content[0].text.strip()

                # 清理可能的 markdown fence
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()

                result = json.loads(raw)
                if isinstance(result, list) and len(result) == len(articles):
                    return result
                else:
                    logger.warning(f"[extractor] Unexpected result length: {len(result)} vs {len(articles)}")
                    return None

            except json.JSONDecodeError as e:
                logger.warning(f"[extractor] JSON decode error (attempt {attempt+1}): {e}")
                if attempt == 0:
                    retry_msg = "IMPORTANT: Your previous response was not valid JSON. Return ONLY a valid JSON array, nothing else."
                    continue
                return None
            except anthropic.APITimeoutError:
                logger.warning(f"[extractor] API timeout (attempt {attempt+1}), retrying in 5s...")
                time.sleep(5)
            except anthropic.APIError as e:
                logger.error(f"[extractor] Claude API error: {e}")
                time.sleep(5)
            except Exception as e:
                logger.error(f"[extractor] Unexpected error: {e}", exc_info=True)
                return None

        return None

    result = call_claude(user_prompt)

    if result is None:
        logger.error(f"[extractor] Batch extraction failed for {len(articles)} articles, using defaults")
        return [default_extraction() for _ in articles]

    # 补全缺失字段
    validated = []
    for item in result:
        defaults = default_extraction()
        defaults.update({k: v for k, v in item.items() if v is not None})
        validated.append(defaults)

    return validated


def run():
    """
    主入口：只处理 is_extracted=False 的记录，按 BATCH_SIZE 分批提取。
    """
    from apps.pipeline.models import NewsArticle

    pending = list(NewsArticle.objects.filter(is_extracted=False).order_by("fetched_at"))

    if not pending:
        logger.info("[extractor] No unextracted articles found.")
        return

    logger.info(f"[extractor] Starting extraction for {len(pending)} articles...")
    total_extracted = 0

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i: i + BATCH_SIZE]
        logger.info(f"[extractor] Processing batch {i//BATCH_SIZE + 1}: articles {i+1}-{i+len(batch)}")

        try:
            results = extract_batch(batch)
        except Exception as e:
            logger.error(f"[extractor] Batch error: {e}", exc_info=True)
            results = [default_extraction() for _ in batch]

        for article, extracted in zip(batch, results):
            article.related_tickers = extracted.get("related_tickers", [])
            article.event_type      = extracted.get("event_type", "other")
            article.sentiment       = extracted.get("sentiment", "Neutral")
            article.impact_level    = extracted.get("impact_level", "Low")
            article.one_line_summary = extracted.get("one_line_summary", "")
            article.key_entities    = extracted.get("key_entities", [])
            article.is_extracted    = True
            article.save()
            total_extracted += 1

        logger.info(f"[extractor] Batch done. Extracted so far: {total_extracted}")

    logger.info(f"[extractor] Extraction complete. Total: {total_extracted} articles.")
