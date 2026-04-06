import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone as dt_timezone

import anthropic
import requests
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoSnapshotRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone as dj_tz
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.pipeline.models import NewsArticle, DailyReport

logger = logging.getLogger(__name__)

IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}

# ── Alpaca helpers ────────────────────────────────────────────────────────────

CRYPTO_BASES = frozenset({"BTC", "ETH", "SOL", "DOGE", "AVAX", "LTC", "BCH", "LINK", "UNI", "AAVE"})


def _is_crypto(symbol: str) -> bool:
    """Return True if symbol belongs to a known crypto asset."""
    return any(symbol.upper().replace("/", "").startswith(c) for c in CRYPTO_BASES)


def _normalize_crypto_symbol(symbol: str) -> str:
    """Convert BTCUSD / BTC / BTC/USD → BTC/USD (Alpaca SDK format)."""
    clean = symbol.upper().replace("/", "")
    base = next(
        (c for c in sorted(CRYPTO_BASES, key=len, reverse=True) if clean.startswith(c)),
        None,
    )
    return f"{base}/USD" if base else clean


def _trading_client() -> TradingClient:
    return TradingClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=True,
    )


def _stock_data_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )


def _crypto_data_client() -> CryptoHistoricalDataClient:
    return CryptoHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )

CHAT_SYSTEM_PROMPT = """You are a stock trading assistant integrated with Alpaca Paper Trading.
You help users check their portfolio and execute trades through natural conversation.
Current date: {today}

YOUR CAPABILITIES:
- Query current positions and account balance (use get_positions tool)
- Get stock/crypto price snapshot (use get_stock_snapshot tool)
- Buy/sell stocks (use place_order tool) — ALWAYS confirm before executing
- Buy/sell crypto 24/7: BTCUSD, ETHUSD, SOLUSD, DOGEUSD, AVAXUSD, LTCUSD (use place_order tool)
  NOTE: Stocks only trade weekdays 9:30am-4pm ET. Crypto trades 24/7.

CRYPTO SYMBOL FORMAT: use BTCUSD (not BTC/USD or BTC)

STRICT SAFETY RULES:
1. ALWAYS confirm before executing ANY trade. Show confirmation format below and set requires_confirmation=true.
   Confirmation format:
   "确认下单信息：
    操作：[买入/卖出]
    股票：[TICKER]
    数量：[X] 股
    类型：市价单
    请回复「确认」执行，或「取消」放弃。"

2. Execute trade ONLY when user's message is exactly "确认" / "是" / "yes" / "confirm"
   Cancel trade if: "算了" / "取消" / "no" / "cancel"

3. This is PAPER TRADING — always mention this if user asks about real money
4. You are an EXECUTION assistant, NOT a financial advisor
   NEVER suggest which stocks to buy or predict prices
   NEVER make specific investment recommendations

LANGUAGE: Respond in Chinese unless user writes in English."""

TOOLS = [
    {
        "name": "get_positions",
        "description": "Get all current portfolio positions from Alpaca paper trading account",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_account",
        "description": "Get account balance, buying power, and portfolio value",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_stock_snapshot",
        "description": "Get latest price and market data for a stock",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "place_order",
        "description": "Place a market order to buy or sell a stock. Only call this after user confirms.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "qty": {"type": "number"},
                "side": {"type": "string", "enum": ["buy", "sell"]}
            },
            "required": ["symbol", "qty", "side"]
        }
    },
    {
        "name": "close_position",
        "description": "Close (sell all) an existing position. Only call after user confirms.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_price_chart",
        "description": "Get historical daily price data for a stock or crypto to display as a chart. Use when user asks about price trend, chart, or historical prices over N days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA or BTCUSD"},
                "days": {"type": "integer", "description": "Number of days of history, e.g. 7, 30, 90"}
            },
            "required": ["symbol", "days"]
        }
    }
]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """执行工具调用，返回结果字符串给 Claude。使用 alpaca-py SDK。"""
    try:
        if tool_name == "get_positions":
            positions = _trading_client().get_all_positions()
            if not positions:
                return "当前无持仓。"
            lines = [
                f"{p.symbol}: {p.qty}股, 均价${p.avg_entry_price}, "
                f"现价${p.current_price}, 盈亏${float(p.unrealized_pl or 0):+.2f}"
                f"({float(p.unrealized_plpc or 0) * 100:+.1f}%)"
                for p in positions
            ]
            return "\n".join(lines)

        elif tool_name == "get_account":
            a = _trading_client().get_account()
            return (f"账户总市值: ${float(a.portfolio_value or 0):,.2f}\n"
                    f"可用现金: ${float(a.cash or 0):,.2f}\n"
                    f"购买力: ${float(a.buying_power or 0):,.2f}")

        elif tool_name == "get_stock_snapshot":
            symbol = tool_input["symbol"].upper()
            if _is_crypto(symbol):
                api_symbol = _normalize_crypto_symbol(symbol)
                snaps = _crypto_data_client().get_crypto_snapshot(
                    CryptoSnapshotRequest(symbol_or_symbols=api_symbol)
                )
                snap = snaps.get(api_symbol)
                lt = snap.latest_trade if snap else None
                dp = snap.daily_bar if snap else None
                return (f"{symbol} 最新价: ${lt.price if lt else 'N/A'}\n"
                        f"今日: 开盘${dp.open if dp else 'N/A'} "
                        f"最高${dp.high if dp else 'N/A'} 最低${dp.low if dp else 'N/A'}")
            else:
                snaps = _stock_data_client().get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=symbol)
                )
                snap = snaps.get(symbol)
                lt = snap.latest_trade if snap else None
                lq = snap.latest_quote if snap else None
                dp = snap.daily_bar if snap else None
                return (f"{symbol} 最新价: ${lt.price if lt else 'N/A'}\n"
                        f"买/卖: ${lq.ask_price if lq else 'N/A'} / ${lq.bid_price if lq else 'N/A'}\n"
                        f"今日涨跌: 开盘${dp.open if dp else 'N/A'} "
                        f"最高${dp.high if dp else 'N/A'} 最低${dp.low if dp else 'N/A'}")

        elif tool_name == "place_order":
            symbol = tool_input["symbol"].upper().replace("/", "")
            crypto = _is_crypto(symbol)
            if crypto:
                symbol = _normalize_crypto_symbol(symbol)
            order = _trading_client().submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=tool_input["qty"],
                side=OrderSide.BUY if tool_input["side"] == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
            ))
            side_cn = "买入" if tool_input["side"] == "buy" else "卖出"
            return f"✅ 订单已提交！{side_cn} {symbol} {tool_input['qty']}股（市价单），订单ID: {str(order.id)[:8]}..."

        elif tool_name == "close_position":
            symbol = tool_input["symbol"].upper()
            _trading_client().close_position(symbol)
            return f"✅ 已平仓 {symbol} 全部持仓。"

        elif tool_name == "get_price_chart":
            symbol = tool_input["symbol"].upper().replace("/", "")
            days = max(3, min(int(tool_input.get("days", 30)), 365))
            end_dt = date.today()
            start_dt = end_dt - timedelta(days=days + 10)

            if _is_crypto(symbol):
                api_symbol = _normalize_crypto_symbol(symbol)
                bar_set = _crypto_data_client().get_crypto_bars(CryptoBarsRequest(
                    symbol_or_symbols=api_symbol,
                    timeframe=TimeFrame.Day,
                    start=datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=dt_timezone.utc),
                    end=datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=dt_timezone.utc),
                    limit=days + 10,
                ))
                raw_bars = bar_set.data.get(api_symbol, [])
                bars = [
                    {"t": str(b.timestamp)[:10], "c": b.close, "h": b.high, "l": b.low}
                    for b in raw_bars
                ]
            else:
                # 股票历史数据走 Alpha Vantage（Alpaca 免费版无历史股票 K 线）
                outputsize = "full" if days > 100 else "compact"
                r = requests.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": "TIME_SERIES_DAILY",
                        "symbol": symbol,
                        "outputsize": outputsize,
                        "apikey": settings.ALPHAVANTAGE_KEY,
                    },
                    timeout=15,
                )
                r.raise_for_status()
                ts = r.json().get("Time Series (Daily)") or {}
                sorted_dates = sorted(ts.keys())[-days:]
                bars = [
                    {"t": d, "c": ts[d]["4. close"], "h": ts[d]["2. high"], "l": ts[d]["3. low"]}
                    for d in sorted_dates
                ]

            if len(bars) > days:
                bars = bars[-days:]

            if not bars:
                return f"未能获取 {symbol} 的历史数据（市场可能已关闭或数据暂不可用）。"

            labels = [b["t"][:10] for b in bars]
            closes = [round(float(b["c"]), 4) for b in bars]
            highs  = [round(float(b["h"]), 4) for b in bars]
            lows   = [round(float(b["l"]), 4) for b in bars]
            first, last = closes[0], closes[-1]
            change_pct = (last - first) / first * 100

            chart_payload = {
                "type": "chart_data",
                "symbol": symbol,
                "days": len(bars),
                "labels": labels,
                "closes": closes,
                "highs": highs,
                "lows": lows,
                "change_pct": round(change_pct, 2),
            }
            summary = (f"{symbol} 近 {len(bars)} 天价格走势：起始 ${first:,.4g}，最新 ${last:,.4g}，"
                       f"区间涨跌 {change_pct:+.2f}%。已生成折线图。")
            return json.dumps({"__chart__": chart_payload, "summary": summary}, ensure_ascii=False)

        else:
            return f"未知工具: {tool_name}"

    except Exception as e:
        return f"工具执行失败 ({tool_name}): {str(e)}"


def index(request):
    return render(request, "index.html")


def news_list(request):
    try:
        limit = int(request.GET.get("limit", 20))
    except (ValueError, TypeError):
        limit = 20

    impact_level = request.GET.get("impact_level")
    sentiment = request.GET.get("sentiment")
    source_type = request.GET.get("source_type")

    today = dj_tz.now().date()
    qs = NewsArticle.objects.filter(fetched_at__date=today)
    if impact_level:
        qs = qs.filter(impact_level=impact_level)
    if sentiment:
        qs = qs.filter(sentiment=sentiment)
    if source_type:
        qs = qs.filter(source_type=source_type)

    articles = list(qs)
    articles.sort(key=lambda a: (
        IMPACT_ORDER.get(a.impact_level, 99),
        -(a.published_at.timestamp() if a.published_at else 0),
    ))
    articles = articles[:limit]

    data = [
        {
            "id": a.pk,
            "title": a.title,
            "source_type": a.source_type,
            "source_name": a.source_name,
            "url": a.url,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "sentiment": a.sentiment,
            "impact_level": a.impact_level,
            "event_type": a.event_type,
            "one_line_summary": a.one_line_summary,
            "related_tickers": a.related_tickers,
            "key_entities": a.key_entities,
        }
        for a in articles
    ]
    return JsonResponse({"articles": data, "count": len(data)})


def portfolio(request):
    try:
        from apps.pipeline.reporter import fetch_portfolio
        positions = fetch_portfolio()
        return JsonResponse({"portfolio": positions, "error": None})
    except Exception as e:
        logger.exception("portfolio view error")
        return JsonResponse({"portfolio": [], "error": str(e)})


_collect_running = False


@csrf_exempt
@require_http_methods(["POST"])
def collect(request):
    global _collect_running
    if _collect_running:
        return JsonResponse({"status": "ok", "message": "采集已在运行中"})

    def run_pipeline():
        global _collect_running
        _collect_running = True
        try:
            from apps.pipeline.collector import run as collect_run
            from apps.pipeline.extractor import run as extract_run
            collect_run(force=True)
            extract_run()
        except Exception as e:
            logger.exception("collect pipeline error")
        finally:
            _collect_running = False
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass

    t = threading.Thread(target=run_pipeline, daemon=True)
    t.start()
    return JsonResponse({"status": "ok", "message": "采集已在后台启动"})


@csrf_exempt
@require_http_methods(["POST"])
def report_generate(request):
    try:
        from apps.pipeline.reporter import run
        obj = run()
        return JsonResponse({"status": "ok", "report_date": str(obj.report_date)})
    except Exception as e:
        logger.exception("report_generate error")
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


def report_latest(request):
    try:
        report = DailyReport.objects.order_by("-report_date").first()
        if not report:
            return JsonResponse({"error": "No report found"}, status=404)
        return JsonResponse(report.report_data)
    except Exception as e:
        logger.exception("report_latest error")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def chat(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        body = json.loads(request.body)
        user_message = body.get("message", "").strip()
        history = body.get("history", [])

        if not user_message:
            return JsonResponse({"reply": "请输入消息", "requires_confirmation": False, "action_type": "info"})

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        today = date.today().strftime("%Y-%m-%d")
        system = CHAT_SYSTEM_PROMPT.format(today=today)

        # Build messages from history + new message
        messages = []
        for h in history[-10:]:  # keep last 10 turns
            if h.get("role") in ("user", "assistant"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_message})

        # Agentic loop: Claude may call tools
        requires_confirmation = False
        action_type = "info"
        reply = "抱歉，出现了未知错误，请重试。"
        chart_data = None

        for _ in range(5):  # max 5 tool calls per turn
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                # Extract text reply
                reply = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        reply += block.text
                # Detect if reply contains confirmation prompt
                if "确认下单信息" in reply or "请回复「确认」" in reply:
                    requires_confirmation = True
                    action_type = "trade_confirm"
                break

            elif response.stop_reason == "tool_use":
                # Execute all tool calls
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        raw = execute_tool(block.name, block.input)
                        # Check if this is a chart tool result
                        if block.name == "get_price_chart":
                            try:
                                parsed = json.loads(raw)
                                if "__chart__" in parsed:
                                    chart_data = parsed["__chart__"]
                                    raw = parsed.get("summary", raw)
                            except Exception:
                                pass
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": raw,
                        })
                        if block.name in ("place_order", "close_position"):
                            action_type = "trade_executed"

                # Add assistant message + tool results to messages
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            else:
                reply = "抱歉，出现了未知错误，请重试。"
                break
        else:
            reply = "处理超时，请重试。"

        return JsonResponse({
            "reply": reply,
            "requires_confirmation": requires_confirmation,
            "action_type": action_type,
            "chart_data": chart_data,
        })

    except Exception as e:
        logger.error(f"[chat] error: {e}", exc_info=True)
        return JsonResponse({"reply": f"服务错误：{str(e)}", "requires_confirmation": False, "action_type": "error"}, status=500)
