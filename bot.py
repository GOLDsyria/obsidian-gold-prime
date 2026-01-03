import os
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Dict

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("obsidian")

# =========================
# ENV
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # must match Pine secret
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# =========================
# DATA
# =========================
@dataclass
class Trade:
    trade_id: str
    asset: str          # e.g. XAUUSD / XAGUSD / EURUSD / BTCUSDT
    exchange: str       # e.g. OANDA / BINANCE
    direction: str      # BUY / SELL
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    bias_15m: str
    confidence: int
    session: str
    status: str         # ACTIVE / WIN / LOSS
    opened_at_utc: str

@dataclass
class Performance:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consec_losses: int = 0

@dataclass
class State:
    # One active trade per asset
    active_trades: Dict[str, Trade]
    # Performance per asset
    perf_by_asset: Dict[str, Performance]

# =========================
# STATE IO
# =========================
def _default_state() -> State:
    return State(active_trades={}, perf_by_asset={})

def load_state() -> State:
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        active_trades_raw = raw.get("active_trades", {})
        active_trades: Dict[str, Trade] = {}
        for asset, t in active_trades_raw.items():
            if t:
                active_trades[asset] = Trade(**t)

        perf_raw = raw.get("perf_by_asset", {})
        perf_by_asset: Dict[str, Performance] = {}
        for asset, p in perf_raw.items():
            perf_by_asset[asset] = Performance(**p)

        return State(active_trades=active_trades, perf_by_asset=perf_by_asset)
    except Exception as e:
        log.warning("State file invalid; resetting. err=%s", e)
        return _default_state()

def save_state(state: State) -> None:
    raw = {
        "active_trades": {a: asdict(t) for a, t in state.active_trades.items()},
        "perf_by_asset": {a: asdict(p) for a, p in state.perf_by_asset.items()},
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

# =========================
# TELEGRAM (Stable HTTP API)
# =========================
async def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram env vars missing; skipping send.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
    except Exception as e:
        log.error("Telegram send failed: %s", e)

def _fmt_signal(trade: Trade) -> str:
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

def _fmt_update(trade: Trade, result: str) -> str:
    return (
        f"{BOT_NAME}\n"
        "Trade Update\n\n"
        f"Asset: {trade.asset}\n"
        f"Direction: {trade.direction}\n"
        f"Result: {result}\n"
        f"Entry: {trade.entry:.2f} | SL: {trade.sl:.2f} | TP1: {trade.tp1:.2f}\n"
        f"Trade ID: {trade.trade_id}\n"
    )

# =========================
# WEBHOOK SCHEMA
# =========================
class TVPayload(BaseModel):
    secret: str
    event: str               # ENTRY or RESOLVE
    trade_id: str
    asset: str               # XAUUSD / XAGUSD / EURUSD / BTCUSDT ...
    exchange: str            # OANDA / BINANCE ...
    direction: str           # BUY / SELL
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    bias_15m: str
    confidence: int
    session: str
    result: Optional[str] = None  # WIN / LOSS (only for RESOLVE)

# =========================
# APP
# =========================
app = FastAPI()
STATE = load_state()
log.info("BOOT OK | bot=%s | state_loaded=yes | active_assets=%d", BOT_NAME, len(STATE.active_trades))

@app.get("/")
def root():
    return {"ok": True, "service": "obsidian-prime"}

@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME}

def _require_secret(secret: str) -> None:
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

def _get_perf(asset: str) -> Performance:
    if asset not in STATE.perf_by_asset:
        STATE.perf_by_asset[asset] = Performance()
    return STATE.perf_by_asset[asset]

@app.post("/tv")
async def tv_webhook(payload: TVPayload):
    global STATE
    _require_secret(payload.secret)

    asset = payload.asset.strip().upper()
    event = payload.event.strip().upper()

    # ENTRY (one trade per asset)
    if event == "ENTRY":
        if asset in STATE.active_trades:
            return {"ok": True, "ignored": True, "reason": "active_trade_exists_for_asset", "asset": asset}

        trade = Trade(
            trade_id=payload.trade_id,
            asset=asset,
            exchange=payload.exchange.strip(),
            direction=payload.direction.strip().upper(),
            entry=payload.entry,
            sl=payload.sl,
            tp1=payload.tp1,
            tp2=payload.tp2,
            tp3=payload.tp3,
            bias_15m=payload.bias_15m.strip().upper(),
            confidence=int(payload.confidence),
            session=payload.session.strip(),
            status="ACTIVE",
            opened_at_utc=datetime.now(timezone.utc).isoformat()
        )

        STATE.active_trades[asset] = trade
        save_state(STATE)

        await tg_send(_fmt_signal(trade))
        return {"ok": True, "status": "active_set", "asset": asset}

    # RESOLVE (close trade for that asset)
    if event == "RESOLVE":
        if asset not in STATE.active_trades:
            return {"ok": True, "ignored": True, "reason": "no_active_trade_for_asset", "asset": asset}

        active = STATE.active_trades[asset]
        if payload.trade_id != active.trade_id:
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch", "asset": asset}

        result = (payload.result or "").strip().upper()
        if result not in ("WIN", "LOSS"):
            raise HTTPException(status_code=400, detail="Invalid result (WIN/LOSS required)")

        perf = _get_perf(asset)
        perf.trades += 1
        if result == "WIN":
            perf.wins += 1
            perf.consec_losses = 0
        else:
            perf.losses += 1
            perf.consec_losses += 1

        active.status = result
        await tg_send(_fmt_update(active, result))

        del STATE.active_trades[asset]
        save_state(STATE)
        return {"ok": True, "closed": True, "asset": asset}

    raise HTTPException(status_code=400, detail="Unknown event (ENTRY/RESOLVE)")

# =========================
# ADMIN / DEBUG (safe with secret)
# =========================
@app.post("/admin/ping")
async def admin_ping(payload: dict):
    _require_secret(payload.get("secret", ""))
    await tg_send(f"{BOT_NAME}\nTelegram test: OK ✅")
    return {"ok": True, "telegram": "sent"}

@app.get("/state")
def state_view():
    # no secret required for read; remove if you want it private
    return {
        "active_assets": list(STATE.active_trades.keys()),
        "active_trades": {a: asdict(t) for a, t in STATE.active_trades.items()},
        "perf_by_asset": {a: asdict(p) for a, p in STATE.perf_by_asset.items()},
    }

@app.post("/admin/reset")
def admin_reset(payload: dict):
    _require_secret(payload.get("secret", ""))
    asset = str(payload.get("asset", "")).strip().upper()

    if asset:
        existed = asset in STATE.active_trades
        if existed:
            del STATE.active_trades[asset]
        save_state(STATE)
        return {"ok": True, "reset": True, "asset": asset, "existed": existed}

    # reset all
    STATE.active_trades = {}
    save_state(STATE)
    return {"ok": True, "reset": True, "asset": "ALL"}
