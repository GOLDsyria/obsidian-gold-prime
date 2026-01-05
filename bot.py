import os
import json
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

APP_VERSION = "6.2.0"

BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = FastAPI()


def _require_env() -> None:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not WEBHOOK_SECRET:
        missing.append("WEBHOOK_SECRET")
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


async def send_telegram(text: str) -> None:
    _require_env()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


def pretty_signal(data: Dict[str, Any]) -> str:
    # نتوقع رسالة من TradingView مثل:
    # {"secret":"...","side":"BUY","symbol":"XAUUSD","tf":"5m","price":"close","sl":"close","tp1":"close","tp2":"close","tp3":"close","note":"..."}
    symbol = str(data.get("symbol", "UNKNOWN"))
    tf = str(data.get("tf", ""))
    side = str(data.get("side", ""))
    price = str(data.get("price", ""))
    sl = str(data.get("sl", ""))
    tp1 = str(data.get("tp1", ""))
    tp2 = str(data.get("tp2", ""))
    tp3 = str(data.get("tp3", ""))

    note = str(data.get("note", "")).strip()

    direction = "🟢 شراء" if side.upper() == "BUY" else ("🔴 بيع" if side.upper() == "SELL" else "🟡 إشارة")
    lines = [
        f"{BOT_NAME}",
        f"{direction}",
        f"الرمز: {symbol}",
    ]
    if tf:
        lines.append(f"الإطار: {tf}")
    if price:
        lines.append(f"السعر: {price}")
    if sl:
        lines.append(f"ستوب: {sl}")
    if tp1:
        lines.append(f"هدف ١: {tp1}")
    if tp2:
        lines.append(f"هدف ٢: {tp2}")
    if tp3:
        lines.append(f"هدف ٣: {tp3}")
    if note:
        lines.append(f"ملاحظة: {note}")

    return "\n".join(lines)


@app.get("/")
def root():
    # صفحة بسيطة بدل Not Found
    return {"ok": True, "bot": BOT_NAME, "version": APP_VERSION}


@app.get("/health")
def health():
    return {"ok": True, "status": "healthy", "version": APP_VERSION}


@app.post("/tv")
async def tv_webhook(request: Request):
    # TradingView يرسل JSON
    try:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=422, detail="Empty body")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise HTTPException(status_code=422, detail="JSON must be an object")
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Invalid JSON")

    secret = str(data.get("secret", ""))
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    msg = pretty_signal(data)

    try:
        await send_telegram(msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"ok": True})


# لتشغيل محليًا:
# uvicorn bot:app --host 0.0.0.0 --port 8000
