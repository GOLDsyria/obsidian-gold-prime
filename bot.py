import os
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Dict

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ConfigDict

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("obsidian")

# =========================
# ENV
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# مهم: على Koyeb بدون Volume قد يختفي state عند إعادة النشر
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# =========================
# DATA
# =========================
@dataclass
class Trade:
    trade_id: str
    asset: str
    exchange: str
    direction: str  # BUY / SELL
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    bias_15m: str
    confidence: int
    session: str
    status: str  # ACTIVE / WIN / LOSS
    opened_at_utc: str

@dataclass
class Performance:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consec_losses: int = 0

@dataclass
class State:
    # صفقة واحدة لكل أصل
    active_trades: Dict[str, Trade]
    perf: Performance

# =========================
# IO
# =========================
def _safe_load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.error("STATE load failed: %s", e)
        return {}

def load_state() -> State:
    raw = _safe_load_json(STATE_FILE)
    at = raw.get("active_trades", {}) or {}
    active_trades: Dict[str, Trade] = {}
    for k, v in at.items():
        try:
            active_trades[k] = Trade(**v)
        except Exception:
            continue
    perf = Performance(**(raw.get("perf", {}) or {}))
    return State(active_trades=active_trades, perf=perf)

def save_state(state: State) -> None:
    raw = {
        "active_trades": {k: asdict(v) for k, v in state.active_trades.items()},
        "perf": asdict(state.perf),
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

# =========================
# TELEGRAM (HTTP API)
# =========================
def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured (missing TELEGRAM_TOKEN/TELEGRAM_CHAT_ID).")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram send failed: %s | %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.error("Telegram send exception: %s", e)
        return False

def format_signal(trade: Trade) -> str:
    return (
        f"{BOT_NAME}\n"
        "Institutional Scalping Signal\n\n"
        f"Asset: {trade.asset}\n"
        f"Exchange: {trade.exchange}\n"
        f"Direction: {trade.direction}\n"
        f"Confidence: {trade.confidence} / 100\n"
        f"Session: {trade.session}\n\n"
        f"Entry: {trade.entry:.2f}\n"
        f"SL: {trade.sl:.2f}\n"
        f"TP1: {trade.tp1:.2f}\n"
        f"TP2: {trade.tp2:.2f}\n"
        f"TP3: {trade.tp3:.2f}\n\n"
        f"HTF Bias (15M): {trade.bias_15m}\n"
        f"Trade ID: {trade.trade_id}\n"
        "Status: ACTIVE ✅"
    )

def format_update(trade: Trade, result: str) -> str:
    emoji = "🏆" if result == "WIN" else "🛑"
    return (
        f"{BOT_NAME}\n"
        f"Trade Update {emoji}\n\n"
        f"Asset: {trade.asset}\n"
        f"Direction: {trade.direction}\n"
        f"Result: {result}\n"
        f"Entry: {trade.entry:.2f} | SL: {trade.sl:.2f} | TP1: {trade.tp1:.2f}\n"
        f"Trade ID: {trade.trade_id}"
    )

# =========================
# WEBHOOK SCHEMA (يدعم مفاتيح قصيرة لتفادي حد 300/JSON)
# long keys: secret,event,trade_id,asset,exchange,direction,entry,sl,tp1,tp2,tp3,bias_15m,confidence,session,result
# short keys: s,e,id,a,x,d,en,sl,t1,t2,t3,b,c,se,r
# =========================
class TVPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    secret: str = Field(alias="s")
    event: str = Field(alias="e")          # ENTRY / RESOLVE
    trade_id: str = Field(alias="id")

    asset: str = Field(alias="a")
    exchange: str = Field(alias="x")
    direction: str = Field(alias="d")      # BUY / SELL

    entry: float = Field(alias="en")
    sl: float = Field(alias="sl")
    tp1: float = Field(alias="t1")
    tp2: float = Field(alias="t2")
    tp3: float = Field(alias="t3")

    bias_15m: str = Field(alias="b")
    confidence: int = Field(alias="c")
    session: str = Field(alias="se")
    result: Optional[str] = Field(default=None, alias="r")

# =========================
# ADMIN SCHEMAS
# =========================
class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str

# =========================
# APP
# =========================
app = FastAPI()
STATE = load_state()
log.info(
    "BOOT OK | bot=%s | state_loaded=yes | active_assets=%s",
    BOT_NAME,
    len(STATE.active_trades),
)

@app.get("/")
def root():
    return {"ok": True, "service": "obsidian-gold-prime", "bot": BOT_NAME}

@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME}

@app.get("/state")
def state_view():
    return {
        "ok": True,
        "active_trades": {k: asdict(v) for k, v in STATE.active_trades.items()},
        "perf": asdict(STATE.perf),
    }

def _auth_or_401(secret: str):
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

@app.post("/admin/ping")
def admin_ping(payload: AdminSecret):
    _auth_or_401(payload.secret)
    sent = tg_send(f"{BOT_NAME}\n✅ Telegram OK (admin/ping)")
    return {"ok": True, "telegram": "sent" if sent else "failed"}

@app.post("/admin/notify")
def admin_notify(payload: AdminNotify):
    _auth_or_401(payload.secret)
    sent = tg_send(f"{BOT_NAME}\n{payload.text}")
    return {"ok": True, "telegram": "sent" if sent else "failed"}

@app.post("/admin/reset")
def admin_reset(payload: AdminSecret):
    _auth_or_401(payload.secret)
    STATE.active_trades = {}
    STATE.perf = Performance()
    save_state(STATE)
    return {"ok": True, "reset": True}

@app.post("/tv")
def tv_webhook(payload: TVPayload):
    _auth_or_401(payload.secret)

    event = payload.event.upper().strip()
    asset_key = (payload.asset or "").upper().strip()

    if not asset_key:
        raise HTTPException(status_code=400, detail="Missing asset")

    # ENTRY
    if event == "ENTRY":
        if asset_key in STATE.active_trades:
            return {"ok": True, "ignored": True, "reason": "active_trade_exists_for_asset", "asset": asset_key}

        trade = Trade(
            trade_id=payload.trade_id,
            asset=asset_key,
            exchange=payload.exchange,
            direction=payload.direction.upper(),
            entry=float(payload.entry),
            sl=float(payload.sl),
            tp1=float(payload.tp1),
            tp2=float(payload.tp2),
            tp3=float(payload.tp3),
            bias_15m=payload.bias_15m,
            confidence=int(payload.confidence),
            session=payload.session,
            status="ACTIVE",
            opened_at_utc=datetime.now(timezone.utc).isoformat(),
        )

        STATE.active_trades[asset_key] = trade
        save_state(STATE)
        tg_send(format_signal(trade))
        return {"ok": True, "status": "active_set", "asset": asset_key}

    # RESOLVE
    if event == "RESOLVE":
        trade = STATE.active_trades.get(asset_key)
        if not trade:
            return {"ok": True, "ignored": True, "reason": "no_active_trade_for_asset", "asset": asset_key}

        if payload.trade_id != trade.trade_id:
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch", "asset": asset_key}

        result = (payload.result or "").upper().strip()
        if result not in ("WIN", "LOSS"):
            raise HTTPException(status_code=400, detail="Invalid result (use WIN/LOSS)")

        # تحديث الأداء (عام)
        STATE.perf.trades += 1
        if result == "WIN":
            STATE.perf.wins += 1
            STATE.perf.consec_losses = 0
        else:
            STATE.perf.losses += 1
            STATE.perf.consec_losses += 1

        trade.status = result
        tg_send(format_update(trade, result))

        # إغلاق الصفقة لهذا الأصل فقط
        del STATE.active_trades[asset_key]
        save_state(STATE)
        return {"ok": True, "closed": True, "asset": asset_key}

    raise HTTPException(status_code=400, detail="Unknown event (use ENTRY/RESOLVE)")
