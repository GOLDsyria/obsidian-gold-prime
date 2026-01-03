import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, AliasChoices, ConfigDict

from telegram import Bot


# =========================
# ENV / Config
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "8f2c9b1a-ChangeMe").strip()

# Only these assets are allowed (as you requested)
ALLOWED_ASSETS = {"XAUUSD", "XAGUSD"}

# One trade per asset policy
# If an asset has an active trade, ignore ENTRY until it is RESOLVED (TP1/SL/manual)
ONE_TRADE_PER_ASSET = True

# Optional: dynamic lot suggestion (does NOT place trades, only suggests)
ENABLE_LOT_SUGGESTION = True
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "500"))          # you said 500$
RISK_PCT = float(os.getenv("RISK_PCT", "0.25")) / 100.0              # 0.25% default

# Approx $ PnL per 1.0 price unit (e.g. $1 move) per 1.0 lot.
# These vary by broker; you can override in ENV if needed.
# You said: XAU 0.02 lot => $1 move => $2 profit, so 1.0 lot => $100 per $1 move.
DOLLARS_PER_1UNIT_PER_LOT = {
    "XAUUSD": float(os.getenv("XAUUSD_DOLLARS_PER_1UNIT_PER_LOT", "100")),
    # Silver differs by broker; common contracts can be huge. Keep it configurable.
    "XAGUSD": float(os.getenv("XAGUSD_DOLLARS_PER_1UNIT_PER_LOT", "100")),
}

# State file (kept inside container; if instance restarts, state resets unless you mount volume)
STATE_PATH = os.getenv("STATE_PATH", "/tmp/obsidian_state.json")


# =========================
# Helpers
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "N/A"
    # Gold can be 2 decimals, silver 3-5; keep general
    return f"{x:.5f}".rstrip("0").rstrip(".")

def compute_lot(asset: str, entry: float, sl: float) -> Optional[float]:
    """
    Suggest lot size based on:
    risk_usd = balance * risk_pct
    stop_distance = abs(entry - sl)  (in price units, e.g. dollars)
    pnl_per_1unit_for_1lot = DOLLARS_PER_1UNIT_PER_LOT[asset]
    lot = risk_usd / (stop_distance * pnl_per_1unit_for_1lot)
    """
    if not ENABLE_LOT_SUGGESTION:
        return None
    if asset not in DOLLARS_PER_1UNIT_PER_LOT:
        return None
    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return None
    risk_usd = ACCOUNT_BALANCE * RISK_PCT
    denom = stop_dist * DOLLARS_PER_1UNIT_PER_LOT[asset]
    if denom <= 0:
        return None
    lot = risk_usd / denom
    # Keep realistic clamp (you can change)
    lot = clamp(lot, 0.01, 5.0)
    # round to 0.01 (like MT4/MT5 typical)
    return round(lot, 2)


# =========================
# Pydantic Models
# =========================
class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str

class TVPayload(BaseModel):
    """
    Accept BOTH:
    - long keys: secret, event, trade_id, asset, exchange, ...
    - short keys: s, e, id, a, x, d, en, t1, ...
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

    # Optional result for RESOLVE
    result: Optional[str] = Field(default=None, validation_alias=AliasChoices("result", "r"))


# =========================
# State
# =========================
state: Dict[str, Any] = {
    "active": {},    # asset -> active trade dict
    "history": [],   # list of events
}

def load_state() -> bool:
    try:
        if not os.path.exists(STATE_PATH):
            return False
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "active" in data and "history" in data:
            state["active"] = data.get("active", {}) or {}
            state["history"] = data.get("history", []) or []
            return True
    except Exception:
        pass
    return False

def save_state() -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        # ignore disk errors
        pass

def append_history(item: Dict[str, Any]) -> None:
    state["history"].append(item)
    # keep last 300
    if len(state["history"]) > 300:
        state["history"] = state["history"][-300:]


# =========================
# Telegram
# =========================
bot: Optional[Bot] = None
if TELEGRAM_TOKEN:
    bot = Bot(token=TELEGRAM_TOKEN)

async def tg_send(text: str) -> bool:
    if not bot or not TELEGRAM_CHAT_ID:
        return False
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        return True
    except Exception:
        return False


# =========================
# FastAPI
# =========================
app = FastAPI(title="OBSIDIAN GOLD PRIME", version="1.0.0")

loaded = load_state()
print(f"{utc_now_iso()} | INFO | BOOT OK | bot={BOT_NAME} | state_loaded={'yes' if loaded else 'no'} | active_assets={len(state['active'])}")

def require_admin(secret: str) -> None:
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def normalize_asset(asset: str) -> str:
    return (asset or "").strip().upper()

def normalize_dir(direction: str) -> str:
    d = (direction or "").strip().upper()
    if d in {"BUY", "LONG"}:
        return "BUY"
    if d in {"SELL", "SHORT"}:
        return "SELL"
    return d

def make_entry_message(p: TVPayload, lot: Optional[float]) -> str:
    lines = []
    lines.append(BOT_NAME)
    lines.append(f"📌 Signal: {p.asset} | {normalize_dir(p.direction)}")
    lines.append(f"🏷️ Exchange: {p.exchange} | Session: {p.session}")
    lines.append(f"🧠 Bias(15m): {p.bias_15m} | Confidence: {p.confidence}/100")
    lines.append("")
    lines.append(f"🎯 ENTRY: {fmt_price(p.entry)}")
    lines.append(f"🛑 SL: {fmt_price(p.sl)}")
    lines.append(f"✅ TP1: {fmt_price(p.tp1)}")
    lines.append(f"✅ TP2: {fmt_price(p.tp2)}")
    lines.append(f"✅ TP3: {fmt_price(p.tp3)}")
    if lot is not None:
        lines.append("")
        lines.append(f"📏 Suggested lot (risk {RISK_PCT*100:.2f}%): {lot}")
    lines.append("")
    lines.append(f"🆔 Trade: {p.trade_id}")
    lines.append(f"⏱️ {utc_now_iso()}")
    return "\n".join(lines)

def make_resolve_message(p: TVPayload, result: str) -> str:
    lines = []
    lines.append(BOT_NAME)
    lines.append(f"🧾 RESOLVE: {p.asset} | {result}")
    lines.append(f"🆔 Trade: {p.trade_id}")
    lines.append(f"⏱️ {utc_now_iso()}")
    return "\n".join(lines)

@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/state")
def state_view():
    # lightweight status view
    return {
        "ok": True,
        "active": state["active"],
        "history_tail": state["history"][-20:],
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
async def tv_webhook(payload: TVPayload, request: Request):
    asset = normalize_asset(payload.asset)
    if asset not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Asset not allowed: {asset}")

    ev = (payload.event or "").strip().upper()
    direction = normalize_dir(payload.direction)

    # normalize payload fields
    payload.asset = asset
    payload.direction = direction

    # Entry logic: one trade per asset
    if ev == "ENTRY":
        if ONE_TRADE_PER_ASSET and asset in state["active"]:
            # ignore duplicate/new entry while active
            append_history({
                "ts": utc_now_iso(),
                "type": "IGNORED_ENTRY_ACTIVE",
                "asset": asset,
                "trade_id": payload.trade_id,
            })
            save_state()
            return {"ok": True, "status": "ignored_active", "asset": asset}

        lot = compute_lot(asset, float(payload.entry), float(payload.sl))

        state["active"][asset] = {
            "trade_id": payload.trade_id,
            "asset": asset,
            "exchange": payload.exchange,
            "direction": direction,
            "entry": float(payload.entry),
            "sl": float(payload.sl),
            "tp1": float(payload.tp1),
            "tp2": float(payload.tp2),
            "tp3": float(payload.tp3),
            "bias_15m": payload.bias_15m,
            "confidence": int(payload.confidence),
            "session": payload.session,
            "opened_ts": utc_now_iso(),
            "lot": lot,
        }
        append_history({
            "ts": utc_now_iso(),
            "type": "ENTRY",
            "asset": asset,
            "trade_id": payload.trade_id,
            "direction": direction,
            "confidence": int(payload.confidence),
        })
        save_state()

        sent = await tg_send(make_entry_message(payload, lot))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": "sent" if sent else "not_configured"}

    # Resolve logic: close active trade
    if ev == "RESOLVE":
        # result can be TP1/TP2/TP3/SL/MANUAL/etc
        result = (payload.result or "").strip().upper() or "RESOLVED"

        active = state["active"].get(asset)
        if not active:
            append_history({"ts": utc_now_iso(), "type": "RESOLVE_NO_ACTIVE", "asset": asset, "trade_id": payload.trade_id, "result": result})
            save_state()
            return {"ok": True, "status": "no_active", "asset": asset}

        # If trade_id doesn't match, ignore (safety)
        if str(active.get("trade_id")) != str(payload.trade_id):
            append_history({"ts": utc_now_iso(), "type": "RESOLVE_MISMATCH", "asset": asset, "trade_id": payload.trade_id, "active_trade_id": active.get("trade_id")})
            save_state()
            return {"ok": True, "status": "trade_id_mismatch", "asset": asset}

        # Close it
        state["active"].pop(asset, None)
        append_history({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": payload.trade_id, "result": result})
        save_state()

        sent = await tg_send(make_resolve_message(payload, result))
        return {"ok": True, "status": "closed", "asset": asset, "telegram": "sent" if sent else "not_configured"}

    raise HTTPException(status_code=400, detail=f"Unknown event: {ev}")
