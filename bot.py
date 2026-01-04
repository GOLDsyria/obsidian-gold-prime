import os, json, asyncio
from datetime import datetime, timezone, date
from typing import Optional, Dict, Any, Set

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

# every 3 hours by default
REPORT_EVERY_MIN = int(os.getenv("REPORT_EVERY_MIN", "180"))

ALLOWED_ASSETS = {"XAUUSD", "XAGUSD", "BTCUSD", "BTCUSDT"}

# server-side min confidence (Balanced => low)
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "50"))

# =========================
# HELPERS
# =========================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def today_utc() -> str:
    return date.today().isoformat()

def norm_asset(a: str) -> str:
    return (a or "").strip().upper()

def norm_dir(d: str) -> str:
    x = (d or "").strip().upper()
    if x == "LONG": return "BUY"
    if x == "SHORT": return "SELL"
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
    if not expected:
        raise HTTPException(status_code=500, detail="Server secret not set")
    if (given or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

def blank_stats() -> Dict[str, float]:
    return {"trades": 0.0, "wins": 0.0, "losses": 0.0, "r_sum": 0.0}

def ensure_bucket(root: Dict[str, Any], key: str) -> Dict[str, float]:
    if key not in root or not isinstance(root[key], dict):
        root[key] = blank_stats()
    for k in ("trades", "wins", "losses", "r_sum"):
        root[key].setdefault(k, 0.0)
    return root[key]

def winrate(b: Dict[str, float]) -> float:
    t = b.get("trades", 0.0)
    return (b.get("wins", 0.0) / t * 100.0) if t else 0.0

# =========================
# STATE
# =========================
state: Dict[str, Any] = {
    "active": {},          # asset -> trade dict
    "last_price": {},      # asset -> {"price": float, "ts": str}
    "history": [],         # last events
    "perf": {
        "total": blank_stats(),
        "by_setup": {},    # key asset|setup
        "daily": {},       # day -> stats
    },
    "dedupe": {
        "seen": []         # keep last N event keys
    }
}

def load_state():
    global state
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "active" in data:
                state = data
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def hist(e: Dict[str, Any]):
    state["history"].append(e)
    if len(state["history"]) > 800:
        state["history"] = state["history"][-800:]
    save_state()

def dedupe_hit(key: str) -> bool:
    """Return True if already processed recently."""
    seen = state.setdefault("dedupe", {}).setdefault("seen", [])
    if key in seen:
        return True
    seen.append(key)
    if len(seen) > 500:
        state["dedupe"]["seen"] = seen[-500:]
    return False

def record_setup(asset: str, setup: str, result: str):
    res = (result or "").upper()
    is_win = res == "TP1"
    is_loss = res == "SL"
    r = 1.0 if is_win else (-1.0 if is_loss else 0.0)

    total = state["perf"]["total"]
    total["trades"] += 1
    total["r_sum"] += r
    if is_win: total["wins"] += 1
    if is_loss: total["losses"] += 1

    key = f"{asset}|{setup}"
    b = ensure_bucket(state["perf"]["by_setup"], key)
    b["trades"] += 1
    b["r_sum"] += r
    if is_win: b["wins"] += 1
    if is_loss: b["losses"] += 1

    day = today_utc()
    d = ensure_bucket(state["perf"]["daily"], day)
    d["trades"] += 1
    d["r_sum"] += r
    if is_win: d["wins"] += 1
    if is_loss: d["losses"] += 1

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
    confidence: int = Field(default=0, validation_alias=AliasChoices("confidence", "c"))
    session: str = Field(default="ALL", validation_alias=AliasChoices("session", "se"))

    result: Optional[str] = Field(default=None, validation_alias=AliasChoices("result", "r"))
    setup: str = Field(default="CORE", validation_alias=AliasChoices("setup", "st"))
    score: int = Field(default=0, validation_alias=AliasChoices("score", "sc"))

    # optional last price (close) from script
    price: Optional[float] = Field(default=None, validation_alias=AliasChoices("price", "p"))
    why: Optional[str] = Field(default=None, validation_alias=AliasChoices("why", "why"))

# =========================
# APP
# =========================
app = FastAPI(title="OBSIDIAN PRIME", version="5.0.0")
load_state()
print(f"{utc_now_iso()} | INFO | BOOT OK | bot={BOT_NAME} | active_assets={len(state.get('active', {}))}")

# =========================
# MESSAGE FORMAT
# =========================
def msg_entry(p: TVPayload) -> str:
    k = f"{p.asset}|{p.setup}"
    b = state["perf"]["by_setup"].get(k, blank_stats())
    wr = winrate(b)
    t = int(b.get("trades", 0))

    lastp = state.get("last_price", {}).get(p.asset, {})
    lp = lastp.get("price", None)

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
        f"Score: {p.score} | Conf: {p.confidence}\n"
        f"Setup: {p.setup} | Setup WR: {wr:.1f}% ({t})\n"
        f"Last Price: {fmt_price(lp)}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_resolve(p: TVPayload, result: str, setup: str) -> str:
    res = (result or "").upper()
    tag = "✅ WIN (TP1)" if res == "TP1" else "❌ LOSS (SL)" if res == "SL" else f"ℹ️ {res}"
    return (
        f"{BOT_NAME}\n"
        f"🏁 RESOLVE\n"
        f"Asset: {p.asset}\n"
        f"Result: {tag}\n"
        f"Setup: {setup}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_admin(text: str) -> str:
    return f"{BOT_NAME}\n📣 Admin message:\n{text}"

def msg_report() -> str:
    total = state["perf"]["total"]
    t = int(total.get("trades", 0))
    w = int(total.get("wins", 0))
    l = int(total.get("losses", 0))
    wr = (w / t * 100.0) if t else 0.0
    exp = (float(total.get("r_sum", 0.0)) / t) if t else 0.0

    active_assets = list(state.get("active", {}).keys())
    lp = state.get("last_price", {})

    lines = [
        f"{BOT_NAME}",
        "⏱️ 3H Market Pulse",
        f"Active Trades: {', '.join(active_assets) if active_assets else 'None'}",
        f"Performance: Trades={t} | WR={wr:.1f}% | Exp(R)={exp:.3f}",
        "",
        "Last Known Prices:"
    ]
    for a in sorted(ALLOWED_ASSETS):
        x = lp.get(a, {})
        lines.append(f"- {a}: {fmt_price(x.get('price'))}  ({x.get('ts','-')})")

    # Top setups (min 8)
    setups = []
    for k, b in state["perf"].get("by_setup", {}).items():
        tt = int(b.get("trades", 0))
        if tt >= 8:
            setups.append((k, winrate(b), tt))
    setups.sort(key=lambda x: (x[1], x[2]), reverse=True)

    if setups:
        lines += ["", "Top Setup(s):"]
        for k, wr2, tt in setups[:3]:
            lines.append(f"- {k} | WR={wr2:.1f}% ({tt})")

    return "\n".join(lines)

# =========================
# SCHEDULER (3H report)
# =========================
async def reporter_loop():
    # if REPORT_EVERY_MIN <= 0 => disabled
    if REPORT_EVERY_MIN <= 0:
        return
    # slight delay on boot
    await asyncio.sleep(20)
    while True:
        try:
            await tg_send(msg_report())
        except Exception:
            pass
        await asyncio.sleep(REPORT_EVERY_MIN * 60)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(reporter_loop())

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
    exp = (float(total.get("r_sum", 0.0)) / t) if t else 0.0
    return {
        "ok": True,
        "trades": t,
        "wins": w,
        "losses": l,
        "winrate_pct": round(wr, 2),
        "expectancy_R": round(exp, 3),
        "active_assets": list(state.get("active", {}).keys()),
        "report_every_min": REPORT_EVERY_MIN
    }

@app.get("/dashboard")
def dashboard():
    total = state["perf"]["total"]
    t = int(total.get("trades", 0))
    w = int(total.get("wins", 0))
    l = int(total.get("losses", 0))
    wr = (w / t * 100.0) if t else 0.0
    exp = (float(total.get("r_sum", 0.0)) / t) if t else 0.0

    setups = []
    for k, b in state["perf"].get("by_setup", {}).items():
        tt = int(b.get("trades", 0))
        if tt >= 8:
            setups.append({"key": k, "trades": tt, "wr": round(winrate(b), 2), "expR": round(float(b.get("r_sum", 0.0))/tt, 3)})
    setups.sort(key=lambda x: (x["wr"], x["trades"]), reverse=True)

    html = f"""
    <html><head><meta charset="utf-8"><title>{BOT_NAME} Dashboard</title></head>
    <body style="font-family:Arial; padding:18px;">
    <h2>{BOT_NAME} — Dashboard</h2>
    <p><b>Trades:</b> {t} | <b>Wins:</b> {w} | <b>Losses:</b> {l} | <b>Winrate:</b> {round(wr,2)}% | <b>Expectancy(R):</b> {round(exp,3)}</p>
    <p><b>Report every:</b> {REPORT_EVERY_MIN} min</p>
    <p><b>Active assets:</b> {", ".join(state["active"].keys()) if state["active"] else "None"}</p>

    <hr/>
    <h3>Last Prices</h3>
    <pre>{json.dumps(state.get("last_price", {}), ensure_ascii=False, indent=2)}</pre>

    <h3>Top Setups (min 8 trades)</h3>
    <pre>{json.dumps(setups[:10], ensure_ascii=False, indent=2)}</pre>

    <h3>Active Trades</h3>
    <pre>{json.dumps(state.get("active", {}), ensure_ascii=False, indent=2)}</pre>

    <h3>History (last 25)</h3>
    <pre>{json.dumps(state.get("history", [])[-25:], ensure_ascii=False, indent=2)}</pre>
    </body></html>
    """
    return html

# ---- Admin ----
@app.post("/admin/ping")
async def admin_ping(p: AdminSecret):
    require_secret(p.secret, ADMIN_SECRET)
    sent = await tg_send(f"{BOT_NAME}\n✅ Admin ping OK\n{utc_now_iso()}")
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/admin/notify")
async def admin_notify(p: AdminNotify):
    require_secret(p.secret, ADMIN_SECRET)
    sent = await tg_send(msg_admin(p.text))
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

@app.post("/admin/report_now")
async def admin_report_now(p: AdminSecret):
    require_secret(p.secret, ADMIN_SECRET)
    sent = await tg_send(msg_report())
    return {"ok": True, "telegram": "sent" if sent else "not_configured"}

# ---- TradingView webhook ----
@app.post("/tv")
async def tv(p: TVPayload):
    require_secret(p.secret, WEBHOOK_SECRET)

    asset = norm_asset(p.asset)
    if asset not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Asset not allowed: {asset}")

    p.asset = asset
    p.direction = norm_dir(p.direction)
    p.setup = (p.setup or "CORE").strip().upper()
    p.session = (p.session or "ALL").strip()

    # DEDUPE key
    ev = (p.event or "").strip().upper()
    r = (p.result or "").strip().upper() if p.result else ""
    dkey = f"{asset}|{ev}|{p.trade_id}|{r}"
    if dedupe_hit(dkey):
        return {"ok": True, "ignored": True, "reason": "duplicate_event"}

    # Update last known price (from script)
    if p.price is not None:
        state.setdefault("last_price", {})[asset] = {"price": float(p.price), "ts": utc_now_iso()}
        save_state()

    # ENTRY
    if ev == "ENTRY":
        if int(p.confidence) < MIN_CONFIDENCE:
            hist({"ts": utc_now_iso(), "type": "ENTRY_SKIP", "asset": asset, "reason": "low_confidence", "c": p.confidence})
            return {"ok": True, "ignored": True, "reason": "low_confidence"}

        if asset in state["active"]:
            hist({"ts": utc_now_iso(), "type": "ENTRY_IGNORED_ACTIVE", "asset": asset, "incoming": p.trade_id, "active": state["active"][asset].get("trade_id")})
            return {"ok": True, "ignored": True, "reason": "active_trade_exists"}

        state["active"][asset] = {
            "trade_id": p.trade_id,
            "asset": asset,
            "exchange": p.exchange,
            "direction": p.direction,
            "entry": p.entry,
            "sl": p.sl,
            "tp1": p.tp1,
            "tp2": p.tp2,
            "tp3": p.tp3,
            "bias_15m": p.bias_15m,
            "confidence": int(p.confidence),
            "score": int(p.score),
            "setup": p.setup,
            "session": p.session,
            "opened_ts": utc_now_iso(),
        }
        hist({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": p.trade_id, "setup": p.setup, "score": p.score, "day": today_utc()})
        save_state()

        sent = await tg_send(msg_entry(p))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": sent}

    # RESOLVE
    if ev == "RESOLVE":
        active = state["active"].get(asset)
        if not active:
            hist({"ts": utc_now_iso(), "type": "RESOLVE_NO_ACTIVE", "asset": asset, "trade_id": p.trade_id})
            return {"ok": True, "ignored": True, "reason": "no_active_trade"}

        if str(active.get("trade_id")) != str(p.trade_id):
            hist({"ts": utc_now_iso(), "type": "RESOLVE_MISMATCH", "asset": asset, "incoming": p.trade_id, "active": active.get("trade_id")})
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        result = (p.result or "").strip().upper()
        setup = active.get("setup", p.setup or "CORE")

        record_setup(asset, setup, result)

        state["active"].pop(asset, None)
        hist({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": p.trade_id, "result": result, "setup": setup, "day": today_utc()})
        save_state()

        sent = await tg_send(msg_resolve(p, result, setup))
        return {"ok": True, "status": "closed", "asset": asset, "result": result, "telegram": sent}

    hist({"ts": utc_now_iso(), "type": "UNKNOWN_EVENT", "asset": asset, "event": ev})
    return {"ok": True, "ignored": True, "reason": "unknown_event"}
