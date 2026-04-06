import json
import logging
from datetime import date

import anthropic
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.shortcuts import render

from apps.pipeline.models import NewsArticle, DailyReport

logger = logging.getLogger(__name__)

IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}

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
    }
]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """执行工具，返回结果字符串给 Claude"""
    import requests as req
    base = settings.ALPACA_BASE_URL  # https://paper-api.alpaca.markets
    headers = {
        "APCA-API-KEY-ID": settings.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
    }

    try:
        if tool_name == "get_positions":
            r = req.get(f"{base}/v2/positions", headers=headers, timeout=10)
            r.raise_for_status()
            positions = r.json()
            if not positions:
                return "当前无持仓。"
            lines = []
            for p in positions:
                pnl = float(p.get('unrealized_pl', 0))
                pnl_pct = float(p.get('unrealized_plpc', 0)) * 100
                lines.append(
                    f"{p['symbol']}: {p['qty']}股, 均价${p['avg_entry_price']}, "
                    f"现价${p['current_price']}, 盈亏${pnl:+.2f}({pnl_pct:+.1f}%)"
                )
            return "\n".join(lines)

        elif tool_name == "get_account":
            r = req.get(f"{base}/v2/account", headers=headers, timeout=10)
            r.raise_for_status()
            a = r.json()
            return (f"账户总市值: ${float(a.get('portfolio_value', 0)):,.2f}\n"
                    f"可用现金: ${float(a.get('cash', 0)):,.2f}\n"
                    f"购买力: ${float(a.get('buying_power', 0)):,.2f}")

        elif tool_name == "get_stock_snapshot":
            symbol = tool_input["symbol"].upper()
            crypto_bases = {"BTC","ETH","SOL","DOGE","AVAX","LTC","BCH","LINK","UNI","AAVE"}
            is_crypto = any(symbol.startswith(c) for c in crypto_bases)
            if is_crypto:
                # Alpaca crypto API requires BTC/USD format
                crypto_symbol = symbol
                if "/" not in crypto_symbol:
                    ticker_base = next(c for c in sorted(crypto_bases, key=len, reverse=True) if crypto_symbol.startswith(c))
                    crypto_symbol = f"{ticker_base}/USD"
                r = req.get(
                    f"https://data.alpaca.markets/v1beta3/crypto/us/snapshots?symbols={crypto_symbol}",
                    headers=headers, timeout=10
                )
                r.raise_for_status()
                data = r.json().get("snapshots", {}).get(crypto_symbol, {})
                lt = data.get("latestTrade", {})
                dp = data.get("dailyBar", {})
                return (f"{symbol} 最新价: ${lt.get('p', 'N/A')}\n"
                        f"今日: 开盘${dp.get('o','N/A')} 最高${dp.get('h','N/A')} 最低${dp.get('l','N/A')}")
            r = req.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/snapshot",
                headers=headers, timeout=10
            )
            r.raise_for_status()
            data = r.json()
            lt = data.get("latestTrade", {})
            lq = data.get("latestQuote", {})
            dp = data.get("dailyBar", {})
            return (f"{symbol} 最新价: ${lt.get('p', 'N/A')}\n"
                    f"买/卖: ${lq.get('ap', 'N/A')} / ${lq.get('bp', 'N/A')}\n"
                    f"今日涨跌: 开盘${dp.get('o', 'N/A')} 最高${dp.get('h', 'N/A')} 最低${dp.get('l', 'N/A')}")

        elif tool_name == "place_order":
            symbol = tool_input["symbol"].upper().replace("/", "")
            # 加密货币识别并转为 Alpaca 格式 BTC/USD
            crypto_bases = {"BTC","ETH","SOL","DOGE","AVAX","LTC","BCH","LINK","UNI","AAVE"}
            is_crypto = any(symbol.startswith(c) for c in crypto_bases)
            if is_crypto and "/" not in symbol:
                # BTCUSD -> BTC/USD
                ticker_base = next(c for c in sorted(crypto_bases, key=len, reverse=True) if symbol.startswith(c))
                symbol = f"{ticker_base}/USD"
            tif = "gtc" if is_crypto else "day"
            payload = {
                "symbol": symbol,
                "qty": str(tool_input["qty"]),
                "side": tool_input["side"],
                "type": "market",
                "time_in_force": tif,
            }
            r = req.post(f"{base}/v2/orders", headers=headers, json=payload, timeout=10)
            r.raise_for_status()
            order = r.json()
            side_cn = "买入" if tool_input["side"] == "buy" else "卖出"
            return f"✅ 订单已提交！{side_cn} {symbol} {tool_input['qty']}股（市价单），订单ID: {order.get('id', '')[:8]}..."

        elif tool_name == "close_position":
            symbol = tool_input["symbol"].upper()
            r = req.delete(f"{base}/v2/positions/{symbol}", headers=headers, timeout=10)
            r.raise_for_status()
            return f"✅ 已平仓 {symbol} 全部持仓。"

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

    qs = NewsArticle.objects.all()
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
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
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
        })

    except Exception as e:
        logger.error(f"[chat] error: {e}", exc_info=True)
        return JsonResponse({"reply": f"服务错误：{str(e)}", "requires_confirmation": False, "action_type": "error"}, status=500)
