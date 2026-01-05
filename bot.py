import os
import json
import logging
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("obsidian-webhook")

# ---------------- Env ----------------
BOT_NAME = os.getenv("BOT_NAME", "OBSIDIAN GOLD PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ChangeMe")

# (اختياري) متغيرات موجودة عندك على Koyeb لكنها ليست مطلوبة للويبهوك
TV_EXCHANGE = os.getenv("TV_EXCHANGE", "")
TV_SYMBOL = os.getenv("TV_SYMBOL", "")

app = FastAPI(title="Obsidian TV Webhook", version="1.0")


def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram env missing. TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        logger.error("Telegram error: %s | %s", r.status_code, r.text)
        raise RuntimeError("Telegram send failed")


@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME}


@app.post("/tv")
async def tv_webhook(request: Request, secret: Optional[str] = None):
    """
    TradingView Webhook endpoint.
    Auth by:
    - Query param: /tv?secret=WEBHOOK_SECRET
      OR
    - JSON field: {"secret":"WEBHOOK_SECRET", ...}
    """

    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", "ignore").strip()

    if not raw_text:
        raise HTTPException(status_code=422, detail="Empty body")

    try:
        data = json.loads(raw_text)
    except Exception:
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid JSON", "body_preview": raw_text[:400]},
        )

    # Auth check
    body_secret = None
    if isinstance(data, dict):
        body_secret = data.get("secret")

    if (secret or body_secret) != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Read fields safely
    symbol = str(data.get("symbol", data.get("ticker", "UNKNOWN")))
    side = str(data.get("side", data.get("action", data.get("signal", "UNKNOWN")))).upper()
    price = data.get("price", data.get("close", None))
    timeframe = str(data.get("timeframe", data.get("tf", data.get("interval", ""))))

    sl = data.get("sl")
    tp1 = data.get("tp1")
    tp2 = data.get("tp2")
    tp3 = data.get("tp3")

    note = str(data.get("note", ""))

    # Format
    emoji = "🟢" if side == "BUY" else ("🔴" if side == "SELL" else "🟡")

    def fmt_num(x: Any) -> str:
        try:
            return f"{float(x):.2f}"
        except Exception:
            return "N/A"

    msg = (
        f"{emoji} <b>{BOT_NAME}</b>\n"
        f"• <b>Symbol:</b> {symbol}\n"
        f"• <b>Side:</b> {side}\n"
        f"• <b>Price:</b> {fmt_num(price)}\n"
        f"• <b>TF:</b> {timeframe}\n"
    )

    # Optional levels
    if sl is not None:
        msg += f"• <b>SL:</b> {fmt_num(sl)}\n"
    if tp1 is not None:
        msg += f"• <b>TP1:</b> {fmt_num(tp1)}\n"
    if tp2 is not None:
        msg += f"• <b>TP2:</b> {fmt_num(tp2)}\n"
    if tp3 is not None:
        msg += f"• <b>TP3:</b> {fmt_num(tp3)}\n"

    if note:
        msg += f"• <b>Note:</b> {note}\n"

    # Send to Telegram
    tg_send(msg)

    logger.info("OK | %s %s price=%s tf=%s", symbol, side, price, timeframe)
    return {"ok": True}
