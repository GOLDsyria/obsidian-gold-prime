import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, AliasChoices, ConfigDict

from telegram import Bot


# =========================
# Config
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "8f2c9b1a-ChangeMe")   # TradingView secret
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "8f2c9b1a-ChangeMe")       # Admin endpoints secret

STATE_FILE = os.getenv("STATE_FILE", "/tmp/obsidian_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("obsidian")

app = FastAPI(title=BOT_NAME)


# =========================
# Models
# =========================
class AdminSecret(BaseModel):
    secret: str


class AdminNotify(BaseModel):
    secret: str
    text: str


class TVPayload(BaseModel):
    """
    Supports BOTH:
      long keys: secret, event, trade_id, asset, exchange, direction, entry, sl, tp1, tp2, tp3, bias_15m, confidence, session, result
      short keys: s, e, id, a, x, d, en, sl, t1, t2, t3, b, c, se, r
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

    # optional: resolve result (TP1/TP2/TP3/SL/CANCEL/BE) etc
    result: Optional[str] = Field(default=None, validation_alias=AliasChoices("result", "r"))


# =========================
# State
# =========================
# One active trade per asset:
# state["active"][asset] = {trade_id, status, ...}
state: Dict[str, Any] = {
    "active": {},     # per-asset current trade
    "history": [],    # last events
    "meta": {
        "bot": BOT_NAME,
        "started_at": None,
        "last_event_at": None
    }
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and "active" in loaded and "history" in loaded:
                state = loaded
                log.info("STATE LOADED | file=%s | active_assets=%s", STATE_FILE, len(state.get("active", {})))
            else:
                log.warning("STATE FILE INVALID STRUCTURE | ignoring")
    except Exception as e:
        log.warning("STATE LOAD FAILED | %s", e)


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("STATE SAVE FAILED | %s", e)


# =========================
# Telegram
# =========================
tg_bot: Optional[Bot] = None


async def tg_send(text: str) -> bool:
    if not tg_bot or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM NOT CONFIGURED | token=%s chat_id=%s", bool(TELEGRAM_TOKEN), bool(TELEGRAM_CHAT_ID))
        return False
    try:
        await tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        return True
    except Exception as e:
        log.error("TELEGRAM SEND FAILED | %s", e)
        return False


def fmt_price(x: float) -> str:
    # Keep readable. Adjust decimals as you like.
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    return f"{x:.4f}"


def build_entry_message(p: TVPayload) -> str:
    # Nice compact message for Telegram
    dir_icon = "🟢 BUY" if p.direction.upper() == "BUY" else "🔴 SELL"
    return (
        f"{BOT_NAME}\n"
        f"🧾 ENTRY SIGNAL\n\n"
        f"Asset: {p.asset} | Exchange: {p.exchange}\n"
        f"Direction: {dir_icon}\n"
        f"Trade ID: {p.trade_id}\n"
        f"Bias(15m): {p.bias_15m} | Confidence: {p.confidence}\n"
        f"Session: {p.session}\n\n"
        f"Entry: {fmt_price(p.entry)}\n"
        f"SL: {fmt_price(p.sl)}\n"
        f"TP1: {fmt_price(p.tp1)}\n"
        f"TP2: {fmt_price(p.tp2)}\n"
        f"TP3: {fmt_price(p.tp3)}\n"
    )


def build_resolve_message(p: TVPayload, result: str) -> str:
    r = (result or "").upper().strip()
    tag = "✅ RESOLVED"
    if r in {"SL", "STOP", "STOPLOSS"}:
        tag = "🛑 STOP LOSS"
    elif r in {"TP1", "TP2", "TP3"}:
        tag = f"🎯 {r} HIT"
    elif r in {"CANCEL"}:
        tag = "⚠️ CANCELLED"
    return (
        f"{BOT_NAME}\n"
        f"{tag}\n\n"
        f"Asset: {p.asset} | Exchange: {p.exchange}\n"
        f"Direction: {p.direction}\n"
        f"Trade ID: {p.trade_id}\n"
        f"Result: {result}\n"
    )


# =========================
# Helpers
# =========================
def require_admin(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_webhook(secret: str):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")


def append_history(item: dict):
    state["history"].append(item)
    # keep history size under control
    if len(state["history"]) > 200:
        state["history"] = state["history"][-200:]


# =========================
# Routes
# =========================
@app.on_event("startup")
async def startup():
    global tg_bot
    state["meta"]["started_at"] = utc_now_iso()
    load_state()
    if TELEGRAM_TOKEN:
        tg_bot = Bot(token=TELEGRAM_TOKEN)
    log.info("BOOT OK | bot=%s | state_loaded=%s | active_assets=%s",
             BOT_NAME, "yes" if state.get("meta", {}).get("started_at") else "no", len(state.get("active", {})))


@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME}


@app.get("/health")
def health():
    return {"ok": True, "status": "healthy", "bot": BOT_NAME}


@app.get("/state")
def state_view():
    return {
        "ok": True,
        "bot": BOT_NAME,
        "active_set": list(state.get("active", {}).keys()),
        "active": state.get("active", {}),
        "history_tail": state.get("history", [])[-10:],
        "meta": state.get("meta", {})
    }


@app.post("/admin/ping")
async def admin_ping(payload: AdminSecret):
    require_admin(payload.secret)
    sent = await tg_send(f"{BOT_NAME}\n✅ Admin ping OK\n{utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}


@app.post("/admin/notify")
async def admin_notify(payload: AdminNotify):
    require_admin(payload.secret)
    sent = await tg_send(f"{BOT_NAME}\n📣 Admin notify:\n{payload.text}")
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
async def tv_webhook(p: TVPayload):
    # Security
    require_webhook(p.secret)

    # Normalize event
    event = p.event.upper().strip()
    asset = p.asset.upper().strip()

    state["meta"]["last_event_at"] = utc_now_iso()

    # Enforce: one active trade per asset
    active_trade = state["active"].get(asset)

    if event == "ENTRY":
        # If there is already an active trade for this asset and it differs -> ignore
        if active_trade and active_trade.get("status") == "OPEN":
            if active_trade.get("trade_id") != p.trade_id:
                append_history({
                    "ts": utc_now_iso(),
                    "type": "ENTRY_IGNORED",
                    "reason": "active_trade_exists",
                    "asset": asset,
                    "incoming_trade_id": p.trade_id,
                    "active_trade_id": active_trade.get("trade_id"),
                })
                save_state()
                return {"ok": True, "ignored": True, "reason": "active_trade_exists"}

        # Set active
        state["active"][asset] = {
            "trade_id": p.trade_id,
            "status": "OPEN",
            "asset": asset,
            "exchange": p.exchange,
            "direction": p.direction,
            "entry": p.entry,
            "sl": p.sl,
            "tp1": p.tp1,
            "tp2": p.tp2,
            "tp3": p.tp3,
            "bias_15m": p.bias_15m,
            "confidence": p.confidence,
            "session": p.session,
            "opened_at": utc_now_iso(),
            "last_update": utc_now_iso(),
        }

        append_history({
            "ts": utc_now_iso(),
            "type": "ENTRY",
            "asset": asset,
            "trade_id": p.trade_id,
            "direction": p.direction,
            "confidence": p.confidence,
        })
        save_state()

        sent = await tg_send(build_entry_message(p))
        return {"ok": True, "telegram": "sent" if sent else "not_configured", "active_set": list(state["active"].keys())}

    elif event in {"RESOLVE", "CLOSE", "EXIT"}:
        # Must have active trade and same trade_id
        if not active_trade or active_trade.get("status") != "OPEN":
            append_history({
                "ts": utc_now_iso(),
                "type": "RESOLVE_IGNORED",
                "reason": "no_active_trade",
                "asset": asset,
                "incoming_trade_id": p.trade_id,
            })
            save_state()
            return {"ok": True, "ignored": True, "reason": "no_active_trade"}

        if active_trade.get("trade_id") != p.trade_id:
            append_history({
                "ts": utc_now_iso(),
                "type": "RESOLVE_IGNORED",
                "reason": "trade_id_mismatch",
                "asset": asset,
                "incoming_trade_id": p.trade_id,
                "active_trade_id": active_trade.get("trade_id"),
            })
            save_state()
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        # Close
        result = p.result or "CLOSE"
        active_trade["status"] = "CLOSED"
        active_trade["result"] = result
        active_trade["closed_at"] = utc_now_iso()
        active_trade["last_update"] = utc_now_iso()

        append_history({
            "ts": utc_now_iso(),
            "type": "RESOLVE",
            "asset": asset,
            "trade_id": p.trade_id,
            "result": result,
        })

        # Remove from active after resolve (or keep? we remove to allow new trade)
        state["active"].pop(asset, None)

        save_state()
        sent = await tg_send(build_resolve_message(p, result))
        return {"ok": True, "telegram": "sent" if sent else "not_configured"}

    else:
        append_history({"ts": utc_now_iso(), "type": "UNKNOWN_EVENT", "event": p.event, "asset": asset})
        save_state()
        return {"ok": True, "ignored": True, "reason": "unknown_event", "event": p.event}
