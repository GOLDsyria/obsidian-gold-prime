import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# Config
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "8f2c9b1a-ChangeMe")  # نفس secret الذي ترسله من Pine/PowerShell
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "8f2c9b1a-ChangeMe")      # يمكن جعله نفس WEBHOOK_SECRET أو مختلف

STATE_PATH = os.getenv("STATE_PATH", "state.json")

# =========================
# FastAPI
# =========================
app = FastAPI(title="Obsidian TV Webhook", version="0.1.0")

# =========================
# Models
# =========================
class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str

class TVPayload(BaseModel):
    secret: str
    event: str               # ENTRY / RESOLVE
    trade_id: str

    asset: str               # XAUUSD / BTCUSDT ...
    exchange: str            # OANDA / BINANCE / TVC ...
    direction: str           # BUY / SELL

    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float

    bias_15m: str
    confidence: int
    session: str
    result: Optional[str] = None  # TP1/TP2/TP3/SL/BE/CANCEL (اختياري)

# =========================
# Helpers
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"active": {}, "history": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active": {}, "history": []}

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

state = load_state()

def append_history(item: Dict[str, Any]) -> None:
    state["history"].append(item)
    # keep last 500
    if len(state["history"]) > 500:
        state["history"] = state["history"][-500:]
    save_state(state)

def require_admin(secret: str) -> None:
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

def require_webhook(secret: str) -> None:
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad_secret")

def normalize_tv_payload(raw: dict) -> dict:
    """
    يقبل: مفاتيح طويلة (secret,event,trade_id,asset,...) أو اختصارات Pine (s,e,id,a,x,d,en,t1,t2,t3,b,c,se,r)
    ويحوّلها إلى الشكل القياسي.
    """
    alias_map = {
        "s": "secret",
        "e": "event",
        "id": "trade_id",
        "a": "asset",
        "x": "exchange",
        "d": "direction",
        "en": "entry",
        "t1": "tp1",
        "t2": "tp2",
        "t3": "tp3",
        "b": "bias_15m",
        "c": "confidence",
        "se": "session",
        "r": "result",
    }
    out = dict(raw)
    for k, v in list(raw.items()):
        if k in alias_map and alias_map[k] not in out:
            out[alias_map[k]] = v
    return out

async def tg_send(text: str) -> bool:
    """
    إرسال رسالة تيليجرام (async) بدون مكتبات إضافية.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    import httpx
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            return r.status_code == 200
    except Exception:
        return False

def fmt_trade(payload: TVPayload) -> str:
    direction_icon = "🟢 BUY" if payload.direction.upper() == "BUY" else "🔴 SELL"
    return (
        f"{BOT_NAME}\n"
        f"📌 <b>{payload.asset}</b> | {direction_icon}\n"
        f"🏦 <b>{payload.exchange}</b> | 🕒 <b>{payload.session}</b>\n\n"
        f"🎯 <b>ENTRY</b>: <code>{payload.entry}</code>\n"
        f"🛑 <b>SL</b>: <code>{payload.sl}</code>\n"
        f"✅ <b>TP1</b>: <code>{payload.tp1}</code>\n"
        f"✅ <b>TP2</b>: <code>{payload.tp2}</code>\n"
        f"✅ <b>TP3</b>: <code>{payload.tp3}</code>\n\n"
        f"🧠 Bias(15m): <b>{payload.bias_15m}</b>\n"
        f"⭐ Confidence: <b>{payload.confidence}</b>\n"
        f"🆔 Trade ID: <code>{payload.trade_id}</code>\n"
        f"⏱ {utc_now_iso()}"
    )

def fmt_resolve(payload: TVPayload, result: str) -> str:
    res = result.upper()
    emoji = "🏁"
    if res in ("TP1", "TP2", "TP3"):
        emoji = "🎯"
    elif res in ("SL",):
        emoji = "🛑"
    elif res in ("BE", "BREAKEVEN"):
        emoji = "🟦"

    return (
        f"{BOT_NAME}\n"
        f"{emoji} <b>RESOLVE</b> | <b>{payload.asset}</b>\n"
        f"🧾 Result: <b>{res}</b>\n"
        f"🆔 Trade ID: <code>{payload.trade_id}</code>\n"
        f"⏱ {utc_now_iso()}"
    )

# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/state")
def view_state():
    # لا تعرض أسرار
    safe = {
        "active_assets": list(state.get("active", {}).keys()),
        "active_set": {k: v.get("trade_id") for k, v in state.get("active", {}).items()},
        "history_len": len(state.get("history", [])),
    }
    return {"ok": True, **safe}

@app.post("/admin/ping")
async def admin_ping(payload: AdminSecret):
    require_admin(payload.secret)
    sent = await tg_send(f"{BOT_NAME}\n✅ Admin ping OK\n⏱ {utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/admin/notify")
async def admin_notify(payload: AdminNotify):
    require_admin(payload.secret)
    sent = await tg_send(f"{BOT_NAME}\n📣 Admin message:\n{payload.text}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/admin/reset")
async def admin_reset(payload: AdminSecret):
    require_admin(payload.secret)
    state["active"] = {}
    append_history({"ts": utc_now_iso(), "type": "ADMIN_RESET"})
    sent = await tg_send(f"{BOT_NAME}\n♻️ State reset done.\n⏱ {utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/tv")
async def tv_webhook(req: Request):
    raw = await req.json()
    data = normalize_tv_payload(raw)

    # تحقق secret (بعد التطبيع)
    secret = data.get("secret", "")
    require_webhook(secret)

    # Parse payload
    try:
        payload = TVPayload(**data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    event = payload.event.upper().strip()
    asset = payload.asset.strip().upper()

    # قاعدة: صفقة واحدة لكل أصل
    # ENTRY:
    if event == "ENTRY":
        existing = state["active"].get(asset)
        if existing and existing.get("trade_id") != payload.trade_id:
            append_history({
                "ts": utc_now_iso(),
                "type": "ENTRY_IGNORED",
                "reason": "active_trade_exists",
                "asset": asset,
                "incoming_trade_id": payload.trade_id,
                "active_trade_id": existing.get("trade_id"),
            })
            return {
                "ok": True,
                "ignored": True,
                "reason": "active_trade_exists",
                "asset": asset,
                "active_trade_id": existing.get("trade_id"),
            }

        # set active
        state["active"][asset] = {
            "trade_id": payload.trade_id,
            "exchange": payload.exchange,
            "direction": payload.direction,
            "entry": payload.entry,
            "sl": payload.sl,
            "tp1": payload.tp1,
            "tp2": payload.tp2,
            "tp3": payload.tp3,
            "bias_15m": payload.bias_15m,
            "confidence": payload.confidence,
            "session": payload.session,
            "opened_at": utc_now_iso(),
            "status": "OPEN",
        }
        save_state(state)
        append_history({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": payload.trade_id})

        sent = await tg_send(fmt_trade(payload))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": sent}

    # RESOLVE:
    if event == "RESOLVE":
        existing = state["active"].get(asset)
        if not existing:
            append_history({
                "ts": utc_now_iso(),
                "type": "RESOLVE_IGNORED",
                "reason": "no_active_trade",
                "asset": asset,
                "trade_id": payload.trade_id,
            })
            return {"ok": True, "ignored": True, "reason": "no_active_trade", "asset": asset}

        if existing.get("trade_id") != payload.trade_id:
            append_history({
                "ts": utc_now_iso(),
                "type": "RESOLVE_IGNORED",
                "reason": "trade_id_mismatch",
                "asset": asset,
                "incoming_trade_id": payload.trade_id,
                "active_trade_id": existing.get("trade_id"),
            })
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        result = (payload.result or "CLOSED").upper()
        # اغلاق الصفقة
        closed = state["active"].pop(asset, None)
        save_state(state)
        append_history({
            "ts": utc_now_iso(),
            "type": "RESOLVE",
            "asset": asset,
            "trade_id": payload.trade_id,
            "result": result,
            "closed": closed,
        })

        sent = await tg_send(fmt_resolve(payload, result))
        return {"ok": True, "status": "closed", "asset": asset, "result": result, "telegram": sent}

    # unknown event
    append_history({"ts": utc_now_iso(), "type": "UNKNOWN_EVENT", "event": payload.event, "asset": asset})
    return {"ok": True, "ignored": True, "reason": "unknown_event", "event": payload.event}
