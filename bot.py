import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# =========================
# Config
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "8f2c9b1a-ChangeMe")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", WEBHOOK_SECRET)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")   # put your real token in Koyeb env
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # channel/group id like -100...

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# =========================
# Helpers
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

async def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
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

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"active": {}, "history": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active": {}, "history": []}

def save_state() -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def append_history(item: Dict[str, Any]) -> None:
    state["history"].append(item)
    # keep last 500
    if len(state["history"]) > 500:
        state["history"] = state["history"][-500:]

def require_admin(secret: str) -> None:
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def normalize_tv_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accept both:
      long keys: secret,event,trade_id,asset,exchange,direction,entry,sl,tp1,tp2,tp3,bias_15m,confidence,session,result
      short keys: s,e,id,a,x,d,en,sl,t1,t2,t3,b,c,se,r
    Convert short -> long (server becomes tolerant even if Pydantic aliases not working).
    """
    m = dict(raw)

    # short -> long
    if "s" in m and "secret" not in m: m["secret"] = m["s"]
    if "e" in m and "event" not in m: m["event"] = m["e"]
    if "id" in m and "trade_id" not in m: m["trade_id"] = m["id"]
    if "a" in m and "asset" not in m: m["asset"] = m["a"]
    if "x" in m and "exchange" not in m: m["exchange"] = m["x"]
    if "d" in m and "direction" not in m: m["direction"] = m["d"]
    if "en" in m and "entry" not in m: m["entry"] = m["en"]
    if "t1" in m and "tp1" not in m: m["tp1"] = m["t1"]
    if "t2" in m and "tp2" not in m: m["tp2"] = m["t2"]
    if "t3" in m and "tp3" not in m: m["tp3"] = m["t3"]
    if "b" in m and "bias_15m" not in m: m["bias_15m"] = m["b"]
    if "c" in m and "confidence" not in m: m["confidence"] = m["c"]
    if "se" in m and "session" not in m: m["session"] = m["se"]
    if "r" in m and "result" not in m: m["result"] = m["r"]

    return m

# =========================
# Models
# =========================
class TVPayload(BaseModel):
    secret: str
    event: str  # ENTRY / RESOLVE
    trade_id: str

    asset: str
    exchange: str
    direction: str  # BUY/SELL

    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float

    bias_15m: str
    confidence: int
    session: str
    result: Optional[str] = None  # TP1/TP2/TP3/SL/CANCEL etc.

class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str = Field(min_length=1, max_length=4000)

# =========================
# App
# =========================
app = FastAPI(title="Obsidian TV Webhook", version="0.2.0")

state = load_state()

@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME, "ts": utc_now_iso()}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/state")
def state_view():
    # no secrets exposed
    return {
        "ok": True,
        "bot": BOT_NAME,
        "active_assets": len(state.get("active", {})),
        "active": state.get("active", {}),
        "history_tail": state.get("history", [])[-30:],
    }

@app.post("/admin/ping")
async def admin_ping(payload: AdminSecret):
    require_admin(payload.secret)
    sent = await tg_send(f"{BOT_NAME}\n✅ Admin ping OK\n{utc_now_iso()}")
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
    save_state()
    sent = await tg_send(f"{BOT_NAME}\n♻️ State reset done.\n{utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/tv")
async def tv_webhook(request: Request):
    raw = await request.json()
    normalized = normalize_tv_payload(raw)

    try:
        payload = TVPayload(**normalized)
    except Exception as e:
        # show what keys came in (helps debugging)
        raise HTTPException(status_code=422, detail={"error": str(e), "received_keys": list(raw.keys())})

    if payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    ev = payload.event.upper().strip()
    asset = payload.asset.upper().strip()

    # Enforce 1 trade per asset
    active = state.setdefault("active", {})

    # ENTRY
    if ev == "ENTRY":
        if asset in active:
            # Ignore if different trade_id already active
            if active[asset].get("trade_id") != payload.trade_id:
                append_history({
                    "ts": utc_now_iso(),
                    "type": "IGNORED",
                    "reason": "trade_id_mismatch",
                    "asset": asset,
                    "incoming_trade_id": payload.trade_id,
                    "active_trade_id": active[asset].get("trade_id"),
                })
                save_state()
                return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

            # Same trade_id re-sent: ignore quietly
            return {"ok": True, "ignored": True, "reason": "duplicate"}

        active[asset] = payload.model_dump()
        append_history({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": payload.trade_id})
        save_state()

        msg = (
            f"{BOT_NAME}\n"
            f"🟢 <b>ENTRY</b>\n"
            f"<b>Asset:</b> {payload.asset}  (<b>{payload.exchange}</b>)\n"
            f"<b>Dir:</b> {payload.direction}\n"
            f"<b>Entry:</b> <code>{payload.entry}</code>\n"
            f"<b>SL:</b> <code>{payload.sl}</code>\n"
            f"<b>TP1:</b> <code>{payload.tp1}</code>\n"
            f"<b>TP2:</b> <code>{payload.tp2}</code>\n"
            f"<b>TP3:</b> <code>{payload.tp3}</code>\n"
            f"<b>Bias 15m:</b> {payload.bias_15m}\n"
            f"<b>Confidence:</b> {payload.confidence}\n"
            f"<b>Session:</b> {payload.session}\n"
            f"<b>ID:</b> <code>{payload.trade_id}</code>\n"
            f"{utc_now_iso()}"
        )
        await tg_send(msg)
        return {"ok": True, "status": "active_set", "asset": asset}

    # RESOLVE
    if ev == "RESOLVE":
        if asset not in active:
            append_history({"ts": utc_now_iso(), "type": "RESOLVE_IGNORED", "reason": "no_active", "asset": asset})
            save_state()
            return {"ok": True, "ignored": True, "reason": "no_active"}

        if active[asset].get("trade_id") != payload.trade_id:
            append_history({
                "ts": utc_now_iso(),
                "type": "RESOLVE_IGNORED",
                "reason": "trade_id_mismatch",
                "asset": asset,
                "incoming_trade_id": payload.trade_id,
                "active_trade_id": active[asset].get("trade_id"),
            })
            save_state()
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        res = (payload.result or "RESOLVED").upper().strip()
        old = active.pop(asset)
        append_history({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": payload.trade_id, "result": res})
        save_state()

        msg = (
            f"{BOT_NAME}\n"
            f"🏁 <b>RESOLVE</b> — <b>{res}</b>\n"
            f"<b>Asset:</b> {payload.asset}\n"
            f"<b>ID:</b> <code>{payload.trade_id}</code>\n"
            f"{utc_now_iso()}"
        )
        await tg_send(msg)
        return {"ok": True, "status": "resolved", "result": res}

    raise HTTPException(status_code=400, detail=f"Unknown event: {payload.event}")
