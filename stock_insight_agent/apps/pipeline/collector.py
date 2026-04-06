"""
apps/pipeline/collector.py

三板块新闻采集模块：
- collect_tech_media():      NewsAPI.org — 科技媒体板块
- collect_financial_news():  Alpha Vantage — 财经新闻板块
- collect_social_media():    Yahoo Finance RSS — 市场讨论板块（替代Reddit）
- run():                     主入口，串行执行，幂等，今日>=20条则跳过
"""

import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Board 1: NewsAPI.org — Tech Media
# ─────────────────────────────────────────────
def collect_tech_media():
    """
    NewsAPI.org 科技板块采集。
    keywords: AI OR semiconductor OR EV OR earnings OR stock
    pageSize: 10, language: en, last 24h
    source_type = 'tech_media'
    """
    from apps.pipeline.models import NewsArticle

    api_key = settings.NEWSAPI_KEY
    if not api_key:
        logger.warning("[collector] NEWSAPI_KEY not set, skipping tech_media")
        return 0

    url = "https://newsapi.org/v2/everything"
    now = datetime.now(timezone.utc)
    from_dt = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "q": "AI OR semiconductor OR EV OR earnings OR stock",
        "language": "en",
        "pageSize": 10,
        "sortBy": "publishedAt",
        "apiKey": api_key,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", [])
    except Exception as e:
        logger.error(f"[collector] tech_media fetch error: {e}")
        return 0

    saved = 0
    for item in articles:
        article_url = (item.get("url") or "").strip()
        if not article_url or article_url == "https://removed.com":
            continue
        title = (item.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue

        content = item.get("content") or item.get("description") or ""
        published_str = item.get("publishedAt", "")
        try:
            pub_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        except Exception:
            pub_dt = dj_timezone.now()

        _, created = NewsArticle.objects.get_or_create(
            url=article_url,
            defaults={
                "source_type": "tech_media",
                "source_name": item.get("source", {}).get("name", "NewsAPI"),
                "title": title,
                "content": content[:5000],
                "published_at": pub_dt,
            },
        )
        if created:
            saved += 1

    logger.info(f"[collector] tech_media: saved {saved} new articles")
    return saved


# ─────────────────────────────────────────────
# Board 2: Alpha Vantage — Financial News
# ─────────────────────────────────────────────
def collect_financial_news():
    """
    Alpha Vantage News Sentiment API。
    topics: technology,earnings,ipo
    limit: 10
    source_type = 'financial_news'
    """
    from apps.pipeline.models import NewsArticle

    api_key = settings.ALPHAVANTAGE_KEY
    if not api_key:
        logger.warning("[collector] ALPHAVANTAGE_KEY not set, skipping financial_news")
        return 0

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "topics": "technology,earnings,ipo",
        "limit": 10,
        "sort": "LATEST",
        "apikey": api_key,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        feed = data.get("feed", [])
    except Exception as e:
        logger.error(f"[collector] financial_news fetch error: {e}")
        return 0

    saved = 0
    for item in feed:
        article_url = (item.get("url") or "").strip()
        if not article_url:
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue

        published_str = item.get("time_published", "")
        try:
            # format: 20240101T120000
            pub_dt = datetime.strptime(published_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            pub_dt = dj_timezone.now()

        content = item.get("summary") or ""

        _, created = NewsArticle.objects.get_or_create(
            url=article_url,
            defaults={
                "source_type": "financial_news",
                "source_name": item.get("source", "Alpha Vantage"),
                "title": title,
                "content": content[:5000],
                "published_at": pub_dt,
            },
        )
        if created:
            saved += 1

    logger.info(f"[collector] financial_news: saved {saved} new articles")
    return saved


# ─────────────────────────────────────────────
# Board 3: Yahoo Finance RSS — Market Discussion
# ─────────────────────────────────────────────
YAHOO_RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,MSFT,NVDA,TSLA,AMZN&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,META,GOOGL,NFLX&region=US&lang=en-US",
    "https://finance.yahoo.com/news/rssindex",
]


def collect_social_media():
    """
    Yahoo Finance RSS 采集，作为市场讨论板块（替代Reddit）。
    抓取多个 RSS Feed，合并去重，取最新10条。
    source_type = 'social_media', source_name = 'Yahoo Finance'
    """
    from apps.pipeline.models import NewsArticle

    headers = {"User-Agent": "Mozilla/5.0 StockInsightAgent/1.0"}
    all_items = []

    for feed_url in YAHOO_RSS_FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            if channel is None:
                continue
            for item in channel.findall("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                description = (item.findtext("description") or "").strip()
                pub_date_str = item.findtext("pubDate") or ""
                try:
                    pub_dt = parsedate_to_datetime(pub_date_str)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = dj_timezone.now()

                if title and link:
                    all_items.append({
                        "title": title,
                        "url": link,
                        "content": description[:5000],
                        "published_at": pub_dt,
                    })
        except Exception as e:
            logger.warning(f"[collector] Yahoo RSS error ({feed_url}): {e}")
            continue

    # Sort descending by published_at, deduplicate by URL, take top 10
    seen = set()
    unique = []
    for item in sorted(all_items, key=lambda x: x["published_at"], reverse=True):
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
        if len(unique) >= 10:
            break

    saved = 0
    for item in unique:
        _, created = NewsArticle.objects.get_or_create(
            url=item["url"],
            defaults={
                "source_type": "social_media",
                "source_name": "Yahoo Finance",
                "title": item["title"],
                "content": item["content"],
                "published_at": item["published_at"],
            },
        )
        if created:
            saved += 1

    logger.info(f"[collector] social_media (Yahoo Finance): saved {saved} new articles")
    return saved


# ─────────────────────────────────────────────
# Main Entry
# ─────────────────────────────────────────────
def run():
    """
    主入口。串行执行三个采集函数。
    幂等：URL 为唯一键，使用 get_or_create。
    今日已有 20+ 条则跳过。
    任一采集失败记录日志，不中断整体流程。
    """
    from apps.pipeline.models import NewsArticle

    today = dj_timezone.now().date()
    today_count = NewsArticle.objects.filter(fetched_at__date=today).count()

    if today_count >= 20:
        logger.info(f"[collector] Today already has {today_count} articles, skipping.")
        return

    logger.info("[collector] Starting data collection...")
    total = 0

    for fn in [collect_tech_media, collect_financial_news, collect_social_media]:
        try:
            n = fn()
            total += n
        except Exception as e:
            logger.error(f"[collector] Unexpected error in {fn.__name__}: {e}", exc_info=True)

    logger.info(f"[collector] Done. Total new articles saved: {total}")
