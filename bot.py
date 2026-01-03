import os
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from telegram import Bot

# ============== LOGGING ==============
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("obsidian")

# ============== ENV ==============
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# ============== DATA ==============
@dataclass
class Trade:
    trade_id: str
    asset: str
    exchange: str
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    bias_15m: str
    confidence: int
    session: str
    status: str
    opened_at_utc: str

@dataclass
class Performance:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consec_losses: int = 0

@dataclass
class State:
    active_trade: Optional[Trade]
    perf: Performance

# ============== IO ==============
def load_state() -> State:
    if not os.path.exists(STATE_FILE):
        return State(active_trade=None, perf=Performance())
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        t = raw.get("active_trade")
        trade = Trade(**t) if t else None
        perf = Performance(**raw.get("perf", {}))
        return State(active_trade=trade, perf=perf)
    except Exception as e:
        log.warning("State file invalid, resetting. err=%s", e)
        return State(active_trade=None, perf=Performance())

def save_state(state: State) -> None:
    raw = {
        "active_trade": asdict(state.active_trade) if state.active_trade else None,
        "perf": asdict(state.perf),
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

# ============== TELEGRAM ==============
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram not configured; skipping send.")
        return
    Bot(token=TELEGRAM_TOKEN).send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

def format_signal(trade: Trade) -> str:
    return (
        f"{BOT_NAME}\n"
        "Institutional Scalping Signal\n\n"
        f"Asset: {trade.asset}\n"
        f"Exchange: {trade.exchange}\n"
        f"Direction: {trade.direction}\n"
        f"Confidence Score: {trade.confidence} / 100\n\n"
        "Entry:\n"
        f"- {trade.entry:.2f}\n\n"
        "Stop Loss:\n"
        f"- {trade.sl:.2f}\n\n"
        "Take Profits:\n"
        f"- TP1: {trade.tp1:.2f}  (≈ +$4–$5)\n"
        f"- TP2: {trade.tp2:.2f}\n"
        f"- TP3: {trade.tp3:.2f}\n\n"
        f"HTF Bias (15M): {trade.bias_15m}\n"
        "Liquidity Event: Confirmed\n"
        "Execution Model: SMC + ICT + SK\n\n"
        "Trade Status: ACTIVE\n"
        f"Trade ID: {trade.trade_id}\n"
    )

def format_update(trade: Trade, result: str) -> str:
    return (
        f"{BOT_NAME}\n"
        "Trade Update\n\n"
        f"Asset: {trade.asset}\n"
        f"Direction: {trade.direction}\n"
        f"Result: {result}\n"
        f"Entry: {trade.entry:.2f} | SL: {trade.sl:.2f} | TP1: {trade.tp1:.2f}\n"
        f"Trade ID: {trade.trade_id}\n"
    )

# ============== WEBHOOK SCHEMA ==============
class TVPayload(BaseModel):
    secret: str
    event: str
    trade_id: str
    asset: str
    exchange: str
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    bias_15m: str
    confidence: int
    session: str
    result: Optional[str] = None

app = FastAPI()
STATE = load_state()
log.info("BOOT: %s | state_loaded=%s", BOT_NAME, "yes")

@app.get("/")
def root():
    return {"ok": True, "service": "obsidian-gold-prime"}

@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME}
    @app.post("/admin/ping")
async def admin_ping(payload: dict):
    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        Bot(token=TELEGRAM_TOKEN).send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"{BOT_NAME}\nTelegram test: OK ✅"
        )
        return {"ok": True, "telegram": "sent"}
    except Exception as e:
        return {"ok": False, "telegram_error": str(e)}

@app.post("/tv")
async def tv_webhook(payload: TVPayload, request: Request):
    global STATE

    if WEBHOOK_SECRET and payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    event = payload.event.upper().strip()

    if event == "ENTRY":
        if STATE.active_trade is not None:
            return {"ok": True, "ignored": True, "reason": "active_trade_exists"}

        trade = Trade(
            trade_id=payload.trade_id,
            asset=payload.asset,
            exchange=payload.exchange,
            direction=payload.direction,
            entry=payload.entry,
            sl=payload.sl,
            tp1=payload.tp1,
            tp2=payload.tp2,
            tp3=payload.tp3,
            bias_15m=payload.bias_15m,
            confidence=payload.confidence,
            session=payload.session,
            status="ACTIVE",
            opened_at_utc=datetime.now(timezone.utc).isoformat()
        )
        STATE.active_trade = trade
        save_state(STATE)
        tg_send(format_signal(trade))
        return {"ok": True, "status": "active_set"}

    if event == "RESOLVE":
        if STATE.active_trade is None:
            return {"ok": True, "ignored": True, "reason": "no_active_trade"}

        if payload.trade_id != STATE.active_trade.trade_id:
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        result = (payload.result or "").upper()
        if result not in ("WIN", "LOSS"):
            raise HTTPException(status_code=400, detail="Invalid result")

        STATE.perf.trades += 1
        if result == "WIN":
            STATE.perf.wins += 1
            STATE.perf.consec_losses = 0
        else:
            STATE.perf.losses += 1
            STATE.perf.consec_losses += 1

        trade = STATE.active_trade
        trade.status = result
        tg_send(format_update(trade, result))

        STATE.active_trade = None
        save_state(STATE)
        return {"ok": True, "closed": True}

    raise HTTPException(status_code=400, detail="Unknown event")
