import os
import json
from datetime import datetime, timezone, date
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, AliasChoices, ConfigDict
import httpx

# =========================
# ENV
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

STATE_PATH = os.getenv("STATE_PATH", "/tmp/obsidian_state.json")

ALLOWED_ASSETS = {"XAUUSD", "XAGUSD", "BTCUSD", "BTCUSDT"}

MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "85"))
# trade rules are enforced in Pine to avoid desync; here we keep only basic safety.

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
    payload = {"chat_id": TELELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            return r.status_code == 200
    except Exception:
        return False

def require_secret(given: str, expected: str) -> None:
    if not expected:
        raise HTTPException(status_code=500, detail="Server secret not set")
    if (given or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

# =========================
# STATE
# =========================
def blank_stats() -> Dict[str, float]:
    return {"trades": 0.0, "wins": 0.0, "losses": 0.0, "r_sum": 0.0}

state: Dict[str, Any] = {
    "active": {},   # asset -> trade dict
    "history": [],  # last events
    "perf": {
        "total": blank_stats(),
        "by_asset": {},
        "by_setup": {},  # key = "<asset>|<setup>"
        "daily": {}
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

def hist(e: Dict[str, Any]) -> None:
    state["history"].append(e)
    if len(state["history"]) > 600:
        state["history"] = state["history"][-600:]
    save_state()

def ensure_bucket(root: Dict[str, Any], key: str) -> Dict[str, float]:
    if key not in root or not isinstance(root[key], dict):
        root[key] = blank_stats()
    for k in ("trades", "wins", "losses", "r_sum"):
        root[key].setdefault(k, 0.0)
    return root[key]

def record_result(asset: str, setup: str, result: str) -> None:
    """
    result: "TP1" => WIN, "SL" => LOSS
    R: TP1=+1, SL=-1 (scalping: we count TP1 as institutional win)
    """
    res = (result or "").upper()
    is_win = res == "TP1"
    is_loss = res == "SL"
    r = 1.0 if is_win else (-1.0 if is_loss else 0.0)

    total = state["perf"]["total"]
    total["trades"] += 1
    total["r_sum"] += r
    if is_win: total["wins"] += 1
    if is_loss: total["losses"] += 1

    b_asset = ensure_bucket(state["perf"]["by_asset"], asset)
    b_asset["trades"] += 1
    b_asset["r_sum"] += r
    if is_win: b_asset["wins"] += 1
    if is_loss: b_asset["losses"] += 1

    setup_key = f"{asset}|{setup}"
    b_setup = ensure_bucket(state["perf"]["by_setup"], setup_key)
    b_setup["trades"] += 1
    b_setup["r_sum"] += r
    if is_win: b_setup["wins"] += 1
    if is_loss: b_setup["losses"] += 1

    day = today_utc()
    b_day = ensure_bucket(state["perf"]["daily"], day)
    b_day["trades"] += 1
    b_day["r_sum"] += r
    if is_win: b_day["wins"] += 1
    if is_loss: b_day["losses"] += 1

def winrate(bucket: Dict[str, float]) -> float:
    t = bucket.get("trades", 0.0)
    if t <= 0:
        return 0.0
    return (bucket.get("wins", 0.0) / t) * 100.0

# =========================
# MODELS (long + short keys)
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
    # Learning fields
    setup: str = Field(default="CORE", validation_alias=AliasChoices("setup", "st"))
    score: int = Field(default=0, validation_alias=AliasChoices("score", "sc"))
    why: Optional[str] = Field(default=None, validation_alias=AliasChoices("why", "why"))

# =========================
# APP
# =========================
app = FastAPI(title="OBSIDIAN PRIME", version="4.0.0")
load_state()
print(f"{utc_now_iso()} | INFO | BOOT OK | bot={BOT_NAME} | active_assets={len(state.get('active', {}))}")

# =========================
# FORMATTERS
# =========================
def msg_entry(p: TVPayload) -> str:
    # “Professional” format: no lot sizing, only price levels + intel + score/setup
    setup_key = f"{p.asset}|{p.setup}"
    b = state["perf"]["by_setup"].get(setup_key, blank_stats())
    wr = winrate(b)
    t = int(b.get("trades", 0))

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
        f"Session: {p.session}\n"
        f"Confidence: {p.confidence} | Score: {p.score}\n"
        f"Setup: {p.setup} | Setup WR: {wr:.1f}% ({t})\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_resolve(p: TVPayload, result: str) -> str:
    res = (result or "RESOLVED").upper()
    tag = "✅ WIN (TP1)" if res == "TP1" else "❌ LOSS (SL)" if res == "SL" else f"ℹ️ {res}"
    return (
        f"{BOT_NAME}\n"
        f"🏁 RESOLVE\n"
        f"Asset: {p.asset}\n"
        f"Result: {tag}\n"
        f"Setup: {p.setup}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_skip(p: TVPayload) -> str:
    why = p.why or "filtered"
    return (
        f"{BOT_NAME}\n"
        f"⏸️ NO TRADE\n"
        f"Asset: {p.asset}\n"
        f"Why: {why}\n"
        f"Bias 15m: {p.bias_15m} | Conf: {p.confidence} | Score: {p.score}\n"
        f"Session: {p.session}\n"
        f"{utc_now_iso()}"
    )

# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME}

@app.get("/metrics")
def metrics():
    total = state["perf"]["total"]
    t = int(total.get("trades", 0))
    w = int(total.get("wins", 0))
    l = int(total.get("losses", 0))
    wr = (w / t * 100.0) if t else 0.0
    rsum = float(total.get("r_sum", 0.0))
    exp = (rsum / t) if t else 0.0
    return {
        "ok": True,
        "trades": t,
        "wins": w,
        "losses": l,
        "winrate_pct": round(wr, 2),
        "expectancy_R": round(exp, 3),
        "active_assets": list(state["active"].keys()),
    }

@app.get("/setups")
def setups():
    # return top/bottom setups by winrate (min trades >= 8)
    items = []
    for k, b in state["perf"]["by_setup"].items():
        t = int(b.get("trades", 0))
        if t < 8:
            continue
        items.append({
            "key": k,
            "trades": t,
            "winrate_pct": round(winrate(b), 2),
            "expectancy_R": round(float(b.get("r_sum", 0.0)) / t, 3)
        })
    items.sort(key=lambda x: (x["winrate_pct"], x["trades"]), reverse=True)
    top = items[:10]
    bottom = list(reversed(items[-10:])) if len(items) >= 10 else []
    return {"ok": True, "top": top, "bottom": bottom}

@app.get("/dashboard")
def dashboard():
    m = metrics()
    # simple HTML
    top = setups()["top"]
    bottom = setups()["bottom"]
    html = f"""
    <html><head><meta charset="utf-8"><title>{BOT_NAME} Dashboard</title></head>
    <body style="font-family:Arial; padding:18px;">
      <h2>{BOT_NAME} — Dashboard</h2>
      <p><b>Trades:</b> {m['trades']} | <b>Wins:</b> {m['wins']} | <b>Losses:</b> {m['losses']} | <b>Winrate:</b> {m['winrate_pct']}% | <b>Expectancy(R):</b> {m['expectancy_R']}</p>
      <p><b>Active assets:</b> {", ".join(m["active_assets"]) if m["active_assets"] else "None"}</p>
      <hr/>
      <h3>Top Setups (min 8 trades)</h3>
      <pre>{json.dumps(top, ensure_ascii=False, indent=2)}</pre>
      <h3>Bottom Setups (min 8 trades)</h3>
      <pre>{json.dumps(bottom, ensure_ascii=False, indent=2)}</pre>
      <hr/>
      <h3>Active trades</h3>
      <pre>{json.dumps(state["active"], ensure_ascii=False, indent=2)}</pre>
      <hr/>
      <h3>History (last 25)</h3>
      <pre>{json.dumps(state["history"][-25:], ensure_ascii=False, indent=2)}</pre>
    </body></html>
    """
    return html

# ---- Admin endpoints ----
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
    state["history"] = []
    state["perf"] = {"total": blank_stats(), "by_asset": {}, "by_setup": {}, "daily": {}}
    save_state()
    sent = await tg_send(f"{BOT_NAME}\n♻️ Reset done\n{utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

# ---- TradingView webhook ----
@app.post("/tv")
async def tv_webhook(payload: TVPayload):
    require_secret(payload.secret, WEBHOOK_SECRET)

    asset = norm_asset(payload.asset)
    if asset not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Asset not allowed: {asset}")

    payload.asset = asset
    payload.direction = norm_dir(payload.direction)
    payload.session = (payload.session or "ALL").strip()
    payload.setup = (payload.setup or "CORE").strip().upper()

    ev = (payload.event or "").strip().upper()

    # Optional safety (keep low risk): ignore very low confidence ENTRY
    if ev == "ENTRY" and int(payload.confidence) < MIN_CONFIDENCE:
        hist({"ts": utc_now_iso(), "type": "SKIP", "asset": asset, "why": "confidence_below_min", "conf": payload.confidence})
        return {"ok": True, "ignored": True, "reason": "confidence_below_min"}

    # SKIP event (from Pine)
    if ev == "SKIP":
        hist({"ts": utc_now_iso(), "type": "SKIP", "asset": asset, "why": payload.why, "score": payload.score, "setup": payload.setup, "day": today_utc()})
        # send skip only if you want (uncomment if you prefer)
        # await tg_send(msg_skip(payload))
        return {"ok": True, "status": "skipped"}

    # ENTRY
    if ev == "ENTRY":
        if asset in state["active"]:
            hist({"ts": utc_now_iso(), "type": "ENTRY_IGNORED_ACTIVE", "asset": asset, "incoming": payload.trade_id, "active": state["active"][asset].get("trade_id")})
            return {"ok": True, "ignored": True, "reason": "active_trade_exists"}

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
            "score": int(payload.score),
            "setup": payload.setup,
            "session": payload.session,
            "opened_ts": utc_now_iso(),
        }
        hist({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": payload.trade_id, "setup": payload.setup, "score": payload.score, "day": today_utc()})
        save_state()

        sent = await tg_send(msg_entry(payload))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": sent}

    # RESOLVE
    if ev == "RESOLVE":
        active = state["active"].get(asset)
        if not active:
            hist({"ts": utc_now_iso(), "type": "RESOLVE_NO_ACTIVE", "asset": asset, "trade_id": payload.trade_id})
            return {"ok": True, "ignored": True, "reason": "no_active_trade"}

        if str(active.get("trade_id")) != str(payload.trade_id):
            hist({"ts": utc_now_iso(), "type": "RESOLVE_MISMATCH", "asset": asset, "incoming": payload.trade_id, "active": active.get("trade_id")})
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        result = (payload.result or "RESOLVED").strip().upper()
        setup = active.get("setup", "CORE")
        session = active.get("session", "ALL")

        # record learning stats
        record_result(asset, setup, result)

        state["active"].pop(asset, None)
        hist({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": payload.trade_id, "result": result, "setup": setup, "session": session, "day": today_utc()})
        save_state()

        sent = await tg_send(msg_resolve(payload, result))
        return {"ok": True, "status": "closed", "asset": asset, "result": result, "telegram": sent}

    raise HTTPException(status_code=400, detail=f"Unknown event: {ev}")
