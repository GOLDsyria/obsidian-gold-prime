import os
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, AliasChoices, ConfigDict
import httpx

# =========================
# ENV
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "8f2c9b1a-ChangeMe").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "8f2c9b1a-ChangeMe").strip()

STATE_PATH = os.getenv("STATE_PATH", "/tmp/obsidian_state.json")

# Only these assets (Gold + Silver + Bitcoin)
ALLOWED_ASSETS = {
    "XAUUSD",
    "XAGUSD",
    "BTCUSD",
    "BTCUSDT",
}

# Policy: one active trade per asset
ONE_TRADE_PER_ASSET = True

# IMPORTANT: as you requested => Telegram message contains ONLY entry/sl/tps + metadata (no lot)
INCLUDE_LOT_IN_TELEGRAM = False  # keep False

# =========================
# UTILS
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def norm_asset(a: str) -> str:
    return (a or "").strip().upper()

def norm_dir(d: str) -> str:
    x = (d or "").strip().upper()
    if x in ("LONG",):
        return "BUY"
    if x in ("SHORT",):
        return "SELL"
    return x

def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "N/A"
    return f"{x:.5f}".rstrip("0").rstrip(".")

async def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            return r.status_code == 200
    except Exception:
        return False

# =========================
# STATE
# =========================
state: Dict[str, Any] = {"active": {}, "history": []}

def load_state() -> None:
    global state
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "active" in data and "history" in data:
                state = data
    except Exception:
        state = {"active": {}, "history": []}

def save_state() -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def hist(event: Dict[str, Any]) -> None:
    state["history"].append(event)
    if len(state["history"]) > 400:
        state["history"] = state["history"][-400:]
    save_state()

# =========================
# MODELS
# =========================
class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str

class TVPayload(BaseModel):
    """
    Accept both long & short payload keys:
    long: secret,event,trade_id,asset,exchange,direction,entry,sl,tp1,tp2,tp3,bias_15m,confidence,session,result
    short: s,e,id,a,x,d,en,sl,t1,t2,t3,b,c,se,r
    """
    model_config = ConfigDict(populate_by_name=True)

    secret: str = Field(validation_alias=AliasChoices("secret", "s"))
    event: str = Field(validation_alias=AliasChoices("event", "e"))
    trade_id: str = Field(validation_alias=AliasChoices("trade_id", "id"))

    asset: str = Field(validation_alias=AliasChoices("asset", "a"))
    exchange: str = Field(validation_alias=AliasChoices("exchange", "x"))
    direction: str = Field(validation_alias=AliasChoices("direction", "d"))

    entry: float = Field(validation_alias=AliasChoices("entry", "en"))
    sl: float = Field(validation_alias=AliasChoices("sl", "sl"))
    tp1: float = Field(validation_alias=AliasChoices("tp1", "t1"))
    tp2: float = Field(validation_alias=AliasChoices("tp2", "t2"))
    tp3: float = Field(validation_alias=AliasChoices("tp3", "t3"))

    bias_15m: str = Field(validation_alias=AliasChoices("bias_15m", "b"))
    confidence: int = Field(validation_alias=AliasChoices("confidence", "c"))
    session: str = Field(validation_alias=AliasChoices("session", "se"))

    result: Optional[str] = Field(default=None, validation_alias=AliasChoices("result", "r"))

# =========================
# APP
# =========================
app = FastAPI(title="OBSIDIAN PRIME", version="2.0.0")

load_state()
print(f"{utc_now_iso()} | INFO | BOOT OK | bot={BOT_NAME} | active_assets={len(state.get('active', {}))}")

def require_secret(given: str, expected: str) -> None:
    if (given or "").strip() != (expected or "").strip():
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/state")
def get_state():
    # safe view (no secrets)
    return {
        "ok": True,
        "active_assets": list(state["active"].keys()),
        "active": state["active"],
        "history_tail": state["history"][-25:],
    }

# ---- Admin endpoints ----
@app.post("/admin/ping")
async def admin_ping(payload: AdminSecret):
    require_secret(payload.secret, ADMIN_SECRET)
    sent = await tg_send(f"{BOT_NAME}\n✅ Admin ping OK\n⏱ {utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/admin/notify")
async def admin_notify(payload: AdminNotify):
    require_secret(payload.secret, ADMIN_SECRET)
    sent = await tg_send(f"{BOT_NAME}\n📣 Admin message:\n{payload.text}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/admin/reset")
async def admin_reset(payload: AdminSecret):
    require_secret(payload.secret, ADMIN_SECRET)
    state["active"] = {}
    hist({"ts": utc_now_iso(), "type": "ADMIN_RESET"})
    sent = await tg_send(f"{BOT_NAME}\n♻️ State reset done\n⏱ {utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

# ---- Telegram formatting ----
def msg_entry(p: TVPayload) -> str:
    # EXACTLY: entry/sl/tp1/tp2/tp3 + bias/conf/session/id
    return (
        f"{BOT_NAME}\n"
        f"🟢 ENTRY\n"
        f"Asset: {p.asset}  ({p.exchange})\n"
        f"Dir: {norm_dir(p.direction)}\n"
        f"Entry: {fmt_price(p.entry)}\n"
        f"SL: {fmt_price(p.sl)}\n"
        f"TP1: {fmt_price(p.tp1)}\n"
        f"TP2: {fmt_price(p.tp2)}\n"
        f"TP3: {fmt_price(p.tp3)}\n"
        f"Bias 15m: {p.bias_15m}\n"
        f"Confidence: {p.confidence}\n"
        f"Session: {p.session}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_resolve(p: TVPayload, res: str) -> str:
    return (
        f"{BOT_NAME}\n"
        f"🏁 RESOLVE\n"
        f"Asset: {p.asset}\n"
        f"Result: {res}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

# ---- TradingView webhook ----
@app.post("/tv")
async def tv_webhook(payload: TVPayload, request: Request):
    require_secret(payload.secret, WEBHOOK_SECRET)

    asset = norm_asset(payload.asset)
    if asset not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Asset not allowed: {asset}")

    payload.asset = asset
    payload.direction = norm_dir(payload.direction)

    ev = (payload.event or "").strip().upper()

    # ENTRY
    if ev == "ENTRY":
        if ONE_TRADE_PER_ASSET and asset in state["active"]:
            hist({
                "ts": utc_now_iso(),
                "type": "ENTRY_IGNORED_ACTIVE",
                "asset": asset,
                "incoming_trade_id": payload.trade_id,
                "active_trade_id": state["active"][asset].get("trade_id"),
            })
            return {"ok": True, "ignored": True, "reason": "active_trade_exists", "asset": asset}

        state["active"][asset] = {
            "trade_id": payload.trade_id,
            "asset": asset,
            "exchange": payload.exchange,
            "direction": payload.direction,
            "entry": payload.entry,
            "sl": payload.sl,
            "tp1": payload.tp1,
            "tp2": payload.tp2,
            "tp3": payload.tp3,
            "bias_15m": payload.bias_15m,
            "confidence": int(payload.confidence),
            "session": payload.session,
            "opened_ts": utc_now_iso(),
        }
        hist({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": payload.trade_id})

        sent = await tg_send(msg_entry(payload))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": sent}

    # RESOLVE
    if ev == "RESOLVE":
        active = state["active"].get(asset)
        if not active:
            hist({"ts": utc_now_iso(), "type": "RESOLVE_NO_ACTIVE", "asset": asset, "trade_id": payload.trade_id})
            return {"ok": True, "ignored": True, "reason": "no_active_trade", "asset": asset}

        if str(active.get("trade_id")) != str(payload.trade_id):
            hist({
                "ts": utc_now_iso(),
                "type": "RESOLVE_MISMATCH",
                "asset": asset,
                "incoming_trade_id": payload.trade_id,
                "active_trade_id": active.get("trade_id"),
            })
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch", "asset": asset}

        result = (payload.result or "RESOLVED").strip().upper()
        state["active"].pop(asset, None)
        hist({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": payload.trade_id, "result": result})

        sent = await tg_send(msg_resolve(payload, result))
        return {"ok": True, "status": "closed", "asset": asset, "result": result, "telegram": sent}

    raise HTTPException(status_code=400, detail=f"Unknown event: {ev}")
