import os
import json
from datetime import datetime, timezone, date
from typing import Optional, Dict, Any, List

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

# Allowed assets: Gold + Silver + Bitcoin
ALLOWED_ASSETS = {"XAUUSD", "XAGUSD", "BTCUSD", "BTCUSDT"}

# Policies (Prop-style)
ONE_TRADE_PER_ASSET = True
MAX_CONSEC_LOSSES = int(os.getenv("MAX_CONSEC_LOSSES", "2"))      # Max 2 consecutive losses
MAX_TRADES_PER_SESSION = int(os.getenv("MAX_TRADES_PER_SESSION", "3"))  # Max 3 per session label

# Confidence gate (server-side safety)
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "90"))

# =========================
# UTILS
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def today_utc() -> str:
    return date.today().isoformat()

def norm_asset(a: str) -> str:
    return (a or "").strip().upper()

def norm_dir(d: str) -> str:
    x = (d or "").strip().upper()
    if x == "LONG":
        return "BUY"
    if x == "SHORT":
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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            return r.status_code == 200
    except Exception:
        return False

def require_secret(given: str, expected: str) -> None:
    if (given or "").strip() != (expected or "").strip():
        raise HTTPException(status_code=401, detail="Unauthorized")

# =========================
# STATE
# =========================
"""
state schema:
- active: asset -> trade dict
- perf:
    - total_trades, wins, losses
    - consec_losses
    - by_asset: asset -> stats
    - by_session: session -> stats
    - daily: YYYY-MM-DD -> stats
"""
def blank_stats() -> Dict[str, int]:
    return {"trades": 0, "wins": 0, "losses": 0}

state: Dict[str, Any] = {
    "active": {},
    "history": [],
    "perf": {
        "total": blank_stats(),
        "consec_losses": 0,
        "by_asset": {},
        "by_session": {},
        "daily": {},
    }
}

def load_state() -> None:
    global state
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "active" in data and "perf" in data and "history" in data:
                state = data
    except Exception:
        pass

def save_state() -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def hist(event: Dict[str, Any]) -> None:
    state["history"].append(event)
    if len(state["history"]) > 500:
        state["history"] = state["history"][-500:]
    save_state()

def ensure_bucket(d: Dict[str, Any], key: str) -> Dict[str, int]:
    if key not in d or not isinstance(d[key], dict):
        d[key] = blank_stats()
    # ensure keys exist
    for k in ("trades", "wins", "losses"):
        d[key].setdefault(k, 0)
    return d[key]

def record_trade_result(asset: str, session: str, result: str) -> None:
    result = (result or "").upper()
    is_win = result in {"TP1", "WIN", "TP"}  # treat TP1 as WIN as agreed
    is_loss = result in {"SL", "LOSS"}

    state["perf"]["total"]["trades"] += 1
    asset_bucket = ensure_bucket(state["perf"]["by_asset"], asset)
    sess_bucket = ensure_bucket(state["perf"]["by_session"], session)
    day_bucket = ensure_bucket(state["perf"]["daily"], today_utc())

    asset_bucket["trades"] += 1
    sess_bucket["trades"] += 1
    day_bucket["trades"] += 1

    if is_win:
        state["perf"]["total"]["wins"] += 1
        asset_bucket["wins"] += 1
        sess_bucket["wins"] += 1
        day_bucket["wins"] += 1
        state["perf"]["consec_losses"] = 0
    elif is_loss:
        state["perf"]["total"]["losses"] += 1
        asset_bucket["losses"] += 1
        sess_bucket["losses"] += 1
        day_bucket["losses"] += 1
        state["perf"]["consec_losses"] += 1

# =========================
# MODELS
# =========================
class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str

class TVPayload(BaseModel):
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
    reason: Optional[str] = Field(default=None, validation_alias=AliasChoices("reason", "why"))  # for SKIP events

# =========================
# APP
# =========================
app = FastAPI(title="OBSIDIAN PRIME", version="3.0.0")

load_state()
print(f"{utc_now_iso()} | INFO | BOOT OK | bot={BOT_NAME} | active_assets={len(state.get('active', {}))}")

# =========================
# FORMATTERS
# =========================
def msg_entry(p: TVPayload) -> str:
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

def msg_resolve(p: TVPayload, result: str) -> str:
    result = (result or "RESOLVED").upper()
    tag = "✅ WIN (TP1)" if result in {"TP1", "WIN", "TP"} else "❌ LOSS (SL)" if result in {"SL", "LOSS"} else f"ℹ️ {result}"
    return (
        f"{BOT_NAME}\n"
        f"🏁 RESOLVE\n"
        f"Asset: {p.asset}\n"
        f"Result: {tag}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_skip(p: TVPayload, why: str) -> str:
    return (
        f"{BOT_NAME}\n"
        f"⏸️ NO TRADE\n"
        f"Asset: {p.asset}\n"
        f"Reason: {why}\n"
        f"Bias 15m: {p.bias_15m} | Confidence: {p.confidence}\n"
        f"Session: {p.session}\n"
        f"{utc_now_iso()}"
    )

def session_trade_count(session: str) -> int:
    # approximate: count today's trades in this session
    day = today_utc()
    day_bucket = state["perf"]["daily"].get(day, {"trades": 0})
    # we also have by_session bucket but across all time; use daily history filter for safety
    count = 0
    for h in reversed(state["history"]):
        if h.get("type") == "RESOLVE" and h.get("day") == day and h.get("session") == session:
            count += 1
    return count

# =========================
# ROUTES
# =========================
@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/state")
def get_state():
    return {
        "ok": True,
        "active": state["active"],
        "perf": state["perf"],
        "history_tail": state["history"][-25:],
    }

@app.get("/metrics")
def metrics():
    # lightweight stats for quick glance
    total = state["perf"]["total"]
    trades = total["trades"]
    wins = total["wins"]
    losses = total["losses"]
    winrate = (wins / trades * 100.0) if trades else 0.0
    return {
        "ok": True,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "consec_losses": state["perf"]["consec_losses"],
        "active_assets": list(state["active"].keys()),
    }

@app.get("/dashboard")
def dashboard():
    m = metrics()
    # simple HTML (works in browser)
    html = f"""
    <html><head><meta charset="utf-8"><title>{BOT_NAME} Dashboard</title></head>
    <body style="font-family:Arial; padding:18px;">
      <h2>{BOT_NAME} — Dashboard</h2>
      <p><b>Trades:</b> {m['trades']} | <b>Wins:</b> {m['wins']} | <b>Losses:</b> {m['losses']} | <b>Winrate:</b> {m['winrate_pct']}%</p>
      <p><b>Consecutive losses:</b> {m['consec_losses']}</p>
      <p><b>Active assets:</b> {", ".join(m["active_assets"]) if m["active_assets"] else "None"}</p>
      <hr/>
      <h3>Active trades</h3>
      <pre>{json.dumps(state["active"], ensure_ascii=False, indent=2)}</pre>
      <hr/>
      <h3>History (last 25)</h3>
      <pre>{json.dumps(state["history"][-25:], ensure_ascii=False, indent=2)}</pre>
    </body></html>
    """
    return html

# ---- Admin ----
@app.post("/admin/ping")
async def admin_ping(payload: AdminSecret):
    require_secret(payload.secret, ADMIN_SECRET)
    sent = await tg_send(f"{BOT_NAME}\n✅ Admin ping OK\n{utc_now_iso()}")
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
    hist({"ts": utc_now_iso(), "type": "ADMIN_RESET", "day": today_utc()})
    sent = await tg_send(f"{BOT_NAME}\n♻️ State reset done\n{utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

# ---- TradingView Webhook ----
@app.post("/tv")
async def tv_webhook(payload: TVPayload, request: Request):
    require_secret(payload.secret, WEBHOOK_SECRET)

    asset = norm_asset(payload.asset)
    if asset not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Asset not allowed: {asset}")

    payload.asset = asset
    payload.direction = norm_dir(payload.direction)

    ev = (payload.event or "").strip().upper()
    session = (payload.session or "ALL").strip()

    # Hard safety gates
    if int(payload.confidence) < MIN_CONFIDENCE and ev == "ENTRY":
        hist({"ts": utc_now_iso(), "type": "SKIP", "asset": asset, "why": "confidence_below_min", "confidence": payload.confidence, "session": session, "day": today_utc()})
        # optional message:
        # await tg_send(msg_skip(payload, "Confidence below minimum"))
        return {"ok": True, "ignored": True, "reason": "confidence_below_min"}

    if state["perf"]["consec_losses"] >= MAX_CONSEC_LOSSES and ev == "ENTRY":
        hist({"ts": utc_now_iso(), "type": "SKIP", "asset": asset, "why": "max_consec_losses", "session": session, "day": today_utc()})
        await tg_send(msg_skip(payload, f"Max consecutive losses reached ({MAX_CONSEC_LOSSES})"))
        return {"ok": True, "ignored": True, "reason": "max_consec_losses"}

    # max trades per session (count resolves today per session)
    if ev == "ENTRY":
        todays_session_count = session_trade_count(session)
        if todays_session_count >= MAX_TRADES_PER_SESSION:
            hist({"ts": utc_now_iso(), "type": "SKIP", "asset": asset, "why": "max_trades_per_session", "session": session, "day": today_utc()})
            await tg_send(msg_skip(payload, f"Max trades per session reached ({MAX_TRADES_PER_SESSION})"))
            return {"ok": True, "ignored": True, "reason": "max_trades_per_session"}

    # SKIP event from Pine (optional)
    if ev == "SKIP":
        why = payload.reason or "filtered"
        hist({"ts": utc_now_iso(), "type": "SKIP", "asset": asset, "why": why, "confidence": payload.confidence, "session": session, "day": today_utc()})
        await tg_send(msg_skip(payload, why))
        return {"ok": True, "status": "skipped", "asset": asset, "why": why}

    # ENTRY
    if ev == "ENTRY":
        if ONE_TRADE_PER_ASSET and asset in state["active"]:
            hist({
                "ts": utc_now_iso(), "type": "ENTRY_IGNORED_ACTIVE",
                "asset": asset, "incoming_trade_id": payload.trade_id,
                "active_trade_id": state["active"][asset].get("trade_id"),
                "session": session, "day": today_utc()
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
            "session": session,
            "opened_ts": utc_now_iso(),
        }
        hist({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": payload.trade_id, "session": session, "day": today_utc()})

        sent = await tg_send(msg_entry(payload))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": sent}

    # RESOLVE
    if ev == "RESOLVE":
        active = state["active"].get(asset)
        if not active:
            hist({"ts": utc_now_iso(), "type": "RESOLVE_NO_ACTIVE", "asset": asset, "trade_id": payload.trade_id, "session": session, "day": today_utc()})
            return {"ok": True, "ignored": True, "reason": "no_active_trade", "asset": asset}

        if str(active.get("trade_id")) != str(payload.trade_id):
            hist({"ts": utc_now_iso(), "type": "RESOLVE_MISMATCH", "asset": asset, "incoming_trade_id": payload.trade_id, "active_trade_id": active.get("trade_id"), "session": session, "day": today_utc()})
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch", "asset": asset}

        result = (payload.result or "RESOLVED").strip().upper()
        state["active"].pop(asset, None)

        # record performance
        record_trade_result(asset, session, result)

        hist({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": payload.trade_id, "result": result, "session": session, "day": today_utc()})
        sent = await tg_send(msg_resolve(payload, result))
        save_state()

        return {"ok": True, "status": "closed", "asset": asset, "result": result, "telegram": sent}

    raise HTTPException(status_code=400, detail=f"Unknown event: {ev}")
