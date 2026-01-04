import os, json, asyncio, csv, io
from datetime import datetime, timezone, date, timedelta
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Response
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

# Default: 30 minutes (you asked: every half-hour)
REPORT_EVERY_MIN = int(os.getenv("REPORT_EVERY_MIN", "30"))
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "55"))

# Allowed assets: Gold/Silver + BTC
ALLOWED_ASSETS = {"XAUUSD", "XAGUSD", "BTCUSD"}

# Circuit breaker
CB_LOOKBACK = int(os.getenv("CB_LOOKBACK", "10"))
CB_MIN_TRADES = int(os.getenv("CB_MIN_TRADES", "8"))
CB_MIN_WR = float(os.getenv("CB_MIN_WR", "35"))
CB_MIN_RSUM = float(os.getenv("CB_MIN_RSUM", "-3.0"))
CB_FREEZE_MIN = int(os.getenv("CB_FREEZE_MIN", "90"))

# Auto-disable weak setups
SETUP_MIN_TRADES = int(os.getenv("SETUP_MIN_TRADES", "12"))
SETUP_MIN_WR = float(os.getenv("SETUP_MIN_WR", "42"))
SETUP_DISABLE_MIN = int(os.getenv("SETUP_DISABLE_MIN", "240"))

# Optional: allow re-entry after TP1 (default OFF)
ALLOW_REENTRY_AFTER_TP1 = os.getenv("ALLOW_REENTRY_AFTER_TP1", "0").strip() == "1"


# =========================
# HELPERS
# =========================
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")

def today_utc() -> str:
    return date.today().isoformat()

def norm_asset(a: str) -> str:
    return (a or "").strip().upper()

def norm_dir(d: str) -> str:
    x = (d or "").strip().upper()
    if x == "LONG": return "BUY"
    if x == "SHORT": return "SELL"
    if x in ("BUY", "SELL"): return x
    return x

def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "N/A"
    s = f"{x:.5f}"
    s = s.rstrip("0").rstrip(".")
    return s

def require_secret(given: str, expected: str) -> None:
    if not expected:
        raise HTTPException(status_code=500, detail="Server secret not set")
    if (given or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

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

def expectancy(b: Dict[str, float]) -> float:
    t = b.get("trades", 0.0)
    return (b.get("r_sum", 0.0) / t) if t else 0.0

def report_label() -> str:
    # nice label: 30m / 3H / 1H ...
    m = max(1, int(REPORT_EVERY_MIN))
    if m % 60 == 0:
        h = m // 60
        return f"{h}H Market Pulse"
    return f"{m}m Market Pulse"


# =========================
# STATE
# =========================
state: Dict[str, Any] = {
    "active": {},           # asset -> trade dict
    "last_price": {},       # asset -> {price, ts}
    "history": [],          # events
    "perf": {"total": blank_stats(), "by_setup": {}, "daily": {}},
    "dedupe": {"seen": []},
    "cb": {"frozen_until": None},
    "disabled_setups": {},  # "XAUUSD|CORE" -> until_iso
    "recent_resolves": []   # list of {ts, asset, setup, result, r}
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
    if len(state["history"]) > 1200:
        state["history"] = state["history"][-1200:]
    save_state()

def dedupe_hit(key: str) -> bool:
    seen = state.setdefault("dedupe", {}).setdefault("seen", [])
    if key in seen:
        return True
    seen.append(key)
    if len(seen) > 800:
        state["dedupe"]["seen"] = seen[-800:]
    return False

def is_cb_frozen() -> bool:
    u = state.get("cb", {}).get("frozen_until")
    if not u:
        return False
    try:
        until = datetime.fromisoformat(u)
        return utc_now() < until
    except Exception:
        return False

def freeze_cb(minutes: int, reason: str):
    until = utc_now() + timedelta(minutes=minutes)
    state.setdefault("cb", {})["frozen_until"] = until.isoformat(timespec="seconds")
    hist({"ts": utc_now_iso(), "type": "CB_FREEZE", "minutes": minutes, "reason": reason})

def setup_disabled(asset: str, setup: str) -> bool:
    key = f"{asset}|{setup}"
    u = state.get("disabled_setups", {}).get(key)
    if not u:
        return False
    try:
        until = datetime.fromisoformat(u)
        if utc_now() >= until:
            state["disabled_setups"].pop(key, None)
            save_state()
            return False
        return True
    except Exception:
        state["disabled_setups"].pop(key, None)
        save_state()
        return False

def disable_setup(asset: str, setup: str, minutes: int, reason: str):
    key = f"{asset}|{setup}"
    until = utc_now() + timedelta(minutes=minutes)
    state.setdefault("disabled_setups", {})[key] = until.isoformat(timespec="seconds")
    hist({"ts": utc_now_iso(), "type": "SETUP_DISABLED", "key": key, "minutes": minutes, "reason": reason})

def check_circuit_breaker():
    rr = state.get("recent_resolves", [])
    if len(rr) < CB_MIN_TRADES:
        return
    window = rr[-CB_LOOKBACK:]
    t = len(window)
    if t < CB_MIN_TRADES:
        return
    wins = sum(1 for x in window if x["result"] == "TP1")
    wr = wins / t * 100.0
    rsum = sum(float(x.get("r", 0.0)) for x in window)
    if wr < CB_MIN_WR or rsum <= CB_MIN_RSUM:
        if not is_cb_frozen():
            freeze_cb(CB_FREEZE_MIN, f"CB: last{t} WR={wr:.1f}% rsum={rsum:.1f}")

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

    rr = state.setdefault("recent_resolves", [])
    rr.append({"ts": utc_now_iso(), "asset": asset, "setup": setup, "result": res, "r": r})
    if len(rr) > 60:
        state["recent_resolves"] = rr[-60:]

    if int(b["trades"]) >= SETUP_MIN_TRADES:
        wr = winrate(b)
        if wr < SETUP_MIN_WR:
            disable_setup(asset, setup, SETUP_DISABLE_MIN, f"Auto-disable: WR {wr:.1f}% < {SETUP_MIN_WR}% ({int(b['trades'])} trades)")

    check_circuit_breaker()
    save_state()


# =========================
# API MODELS
# =========================
class AdminSecret(BaseModel):
    secret: str

class AdminNotify(BaseModel):
    secret: str
    text: str

class TVPayload(BaseModel):
    """
    NOTE:
    - ENTRY/RESOLVE needs trade fields.
    - PRICE only needs (secret,event,asset,exchange,price,time/session/setup optional).
    So we make trade fields optional.
    """
    model_config = ConfigDict(populate_by_name=True)

    secret: str = Field(validation_alias=AliasChoices("secret", "s"))
    event: str = Field(validation_alias=AliasChoices("event", "e"))
    trade_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("trade_id", "id"))

    asset: str = Field(validation_alias=AliasChoices("asset", "a"))
    exchange: str = Field(default="TV", validation_alias=AliasChoices("exchange", "x"))
    direction: Optional[str] = Field(default=None, validation_alias=AliasChoices("direction", "d"))

    entry: Optional[float] = Field(default=None, validation_alias=AliasChoices("entry", "en"))
    sl: Optional[float] = Field(default=None, validation_alias=AliasChoices("sl", "sl"))
    tp1: Optional[float] = Field(default=None, validation_alias=AliasChoices("tp1", "t1"))
    tp2: Optional[float] = Field(default=None, validation_alias=AliasChoices("tp2", "t2"))
    tp3: Optional[float] = Field(default=None, validation_alias=AliasChoices("tp3", "t3"))

    bias_15m: Optional[str] = Field(default=None, validation_alias=AliasChoices("bias_15m", "b"))
    confidence: int = Field(default=0, validation_alias=AliasChoices("confidence", "c"))
    session: str = Field(default="ALL", validation_alias=AliasChoices("session", "se"))

    result: Optional[str] = Field(default=None, validation_alias=AliasChoices("result", "r"))
    setup: str = Field(default="CORE", validation_alias=AliasChoices("setup", "st"))
    score: int = Field(default=0, validation_alias=AliasChoices("score", "sc"))
    price: Optional[float] = Field(default=None, validation_alias=AliasChoices("price", "p"))

    # optional timestamp from TV (if you want)
    t: Optional[str] = Field(default=None, validation_alias=AliasChoices("t", "ts"))


# =========================
# APP
# =========================
app = FastAPI(title="OBSIDIAN PRIME", version="6.1.0")
load_state()


# =========================
# MESSAGE FORMATTERS
# =========================
def msg_entry(p: TVPayload) -> str:
    k = f"{p.asset}|{p.setup}"
    b = state["perf"]["by_setup"].get(k, blank_stats())
    wr = winrate(b)
    t = int(b.get("trades", 0))
    exp = expectancy(b)

    lp = state.get("last_price", {}).get(p.asset, {}).get("price", None)

    return (
        f"{BOT_NAME}\n"
        f"🟢 ENTRY\n"
        f"Asset: {p.asset}  ({p.exchange})\n"
        f"Dir: {norm_dir(p.direction or '')}\n"
        f"Entry: {fmt_price(p.entry)}\n"
        f"SL: {fmt_price(p.sl)}\n"
        f"TP1: {fmt_price(p.tp1)}\n"
        f"TP2: {fmt_price(p.tp2)}\n"
        f"TP3: {fmt_price(p.tp3)}\n"
        f"Bias 15m: {p.bias_15m or 'N/A'}\n"
        f"Session: {p.session}\n"
        f"Score: {p.score} | Conf: {p.confidence}\n"
        f"Setup: {p.setup} | WR: {wr:.1f}% ({t}) | Exp(R): {exp:.3f}\n"
        f"Last Price: {fmt_price(lp)}\n"
        f"ID: {p.trade_id}\n"
        f"{utc_now_iso()}"
    )

def msg_resolve(asset: str, trade_id: str, result: str, setup: str) -> str:
    res = (result or "").upper()
    tag = "✅ WIN (TP1)" if res == "TP1" else "❌ LOSS (SL)" if res == "SL" else f"ℹ️ {res}"
    extra = "Action: Move SL to BE on your platform (if you kept runners)." if res == "TP1" else ""
    return (
        f"{BOT_NAME}\n"
        f"🏁 RESOLVE\n"
        f"Asset: {asset}\n"
        f"Result: {tag}\n"
        f"Setup: {setup}\n"
        f"{extra}\n"
        f"ID: {trade_id}\n"
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

    frozen = is_cb_frozen()
    frozen_until = state.get("cb", {}).get("frozen_until", None)

    active_assets = list(state.get("active", {}).keys())
    lp = state.get("last_price", {})

    lines = [
        f"{BOT_NAME}",
        f"⏱️ {report_label()}",
        f"Circuit Breaker: {'FROZEN' if frozen else 'OK'}" + (f" until {frozen_until}" if frozen and frozen_until else ""),
        f"Active Trades: {', '.join(active_assets) if active_assets else 'None'}",
        f"Performance: Trades={t} | WR={wr:.1f}% | Exp(R)={exp:.3f}",
        "",
        "Last Known Prices:"
    ]
    for a in sorted(ALLOWED_ASSETS):
        x = lp.get(a, {})
        lines.append(f"- {a}: {fmt_price(x.get('price'))}  ({x.get('ts','-')})")

    setups = []
    for k, b in state["perf"].get("by_setup", {}).items():
        tt = int(b.get("trades", 0))
        if tt >= 8:
            setups.append((k, winrate(b), expectancy(b), tt))
    setups.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)

    if setups:
        lines += ["", "Top Setup(s):"]
        for k, wr2, ex2, tt in setups[:3]:
            lines.append(f"- {k} | WR={wr2:.1f}% | Exp={ex2:.3f} ({tt})")

    dis = state.get("disabled_setups", {})
    if dis:
        lines += ["", "Disabled setups:"]
        for k, until in list(dis.items())[:6]:
            lines.append(f"- {k} until {until}")

    return "\n".join(lines)


# =========================
# BACKGROUND REPORTER
# =========================
async def reporter_loop():
    if REPORT_EVERY_MIN <= 0:
        return
    await asyncio.sleep(10)
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
    return {"ok": True, "bot": BOT_NAME, "version": "6.1.0"}

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

@app.get("/stats")
def stats():
    total = state["perf"]["total"]
    t = int(total.get("trades", 0))
    w = int(total.get("wins", 0))
    l = int(total.get("losses", 0))
    wr = (w / t * 100.0) if t else 0.0
    exp = (float(total.get("r_sum", 0.0)) / t) if t else 0.0

    return {
        "bot": BOT_NAME,
        "frozen": is_cb_frozen(),
        "frozen_until": state.get("cb", {}).get("frozen_until"),
        "active": state.get("active", {}),
        "last_price": state.get("last_price", {}),
        "total": {"trades": t, "wins": w, "losses": l, "wr": wr, "exp_r": exp},
        "by_setup": state["perf"].get("by_setup", {}),
        "disabled_setups": state.get("disabled_setups", {})
    }

@app.get("/history")
def history(limit: int = 50):
    limit = max(1, min(300, int(limit)))
    h = state.get("history", [])
    return {"count": len(h), "items": h[-limit:]}

@app.get("/export.csv")
def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ts", "type", "asset", "trade_id", "setup", "score", "result"])
    for e in state.get("history", []):
        w.writerow([
            e.get("ts",""),
            e.get("type",""),
            e.get("asset",""),
            e.get("trade_id", e.get("incoming","")),
            e.get("setup",""),
            e.get("score", e.get("sc","")),
            e.get("result","")
        ])
    data = out.getvalue().encode("utf-8")
    return Response(content=data, media_type="text/csv")


# =========================
# WEBHOOK
# =========================
@app.post("/tv")
async def tv(p: TVPayload):
    require_secret(p.secret, WEBHOOK_SECRET)

    asset = norm_asset(p.asset)
    if asset not in ALLOWED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Asset not allowed: {asset}")

    ev = (p.event or "").strip().upper()
    p.asset = asset
    p.setup = (p.setup or "CORE").strip().upper()
    p.session = (p.session or "ALL").strip()
    if p.direction is not None:
        p.direction = norm_dir(p.direction)

    # -------------------------
    # PRICE event: only update last_price
    # -------------------------
    if ev == "PRICE":
        if p.price is None:
            return {"ok": True, "ignored": True, "reason": "price_missing"}
        state.setdefault("last_price", {})[asset] = {"price": float(p.price), "ts": utc_now_iso()}
        hist({"ts": utc_now_iso(), "type": "PRICE", "asset": asset, "price": float(p.price)})
        save_state()
        return {"ok": True, "status": "price_updated", "asset": asset}

    # -------------------------
    # ENTRY/RESOLVE dedupe
    # -------------------------
    trade_id = (p.trade_id or "").strip()
    r = (p.result or "").strip().upper() if p.result else ""
    dkey = f"{asset}|{ev}|{trade_id}|{r}"
    if dedupe_hit(dkey):
        return {"ok": True, "ignored": True, "reason": "duplicate_event"}

    # -------------------------
    # update last known price if provided on any event
    # -------------------------
    if p.price is not None:
        state.setdefault("last_price", {})[asset] = {"price": float(p.price), "ts": utc_now_iso()}
        save_state()

    # -------------------------
    # global freeze: ignore new entries
    # -------------------------
    if ev == "ENTRY" and is_cb_frozen():
        hist({"ts": utc_now_iso(), "type": "ENTRY_BLOCKED_CB", "asset": asset, "trade_id": trade_id, "setup": p.setup})
        return {"ok": True, "ignored": True, "reason": "circuit_breaker_frozen"}

    # setup disabled: ignore entries for that setup
    if ev == "ENTRY" and setup_disabled(asset, p.setup):
        hist({"ts": utc_now_iso(), "type": "ENTRY_BLOCKED_SETUP", "asset": asset, "trade_id": trade_id, "setup": p.setup})
        return {"ok": True, "ignored": True, "reason": "setup_disabled"}

    # confidence gate
    if ev == "ENTRY" and int(p.confidence) < MIN_CONFIDENCE:
        hist({"ts": utc_now_iso(), "type": "ENTRY_SKIP", "asset": asset, "trade_id": trade_id, "reason": "low_confidence", "c": p.confidence, "setup": p.setup})
        return {"ok": True, "ignored": True, "reason": "low_confidence"}

    # -------------------------
    # ENTRY
    # -------------------------
    if ev == "ENTRY":
        # Validate trade fields (must exist for ENTRY)
        missing = [k for k in ("entry","sl","tp1","tp2") if getattr(p, k) is None]
        if missing:
            hist({"ts": utc_now_iso(), "type": "ENTRY_BAD_PAYLOAD", "asset": asset, "trade_id": trade_id, "missing": missing})
            return {"ok": False, "error": "missing_trade_fields", "missing": missing}

        # One active trade per asset
        if asset in state["active"]:
            # optional re-entry if TP1 already hit and you allow it (only if you design it in Pine logic)
            hist({"ts": utc_now_iso(), "type": "ENTRY_IGNORED_ACTIVE", "asset": asset, "incoming": trade_id, "active": state["active"][asset].get("trade_id"), "setup": p.setup})
            return {"ok": True, "ignored": True, "reason": "active_trade_exists"}

        state["active"][asset] = {
            "trade_id": trade_id,
            "asset": asset,
            "exchange": p.exchange,
            "direction": p.direction or "NA",
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
        hist({"ts": utc_now_iso(), "type": "ENTRY", "asset": asset, "trade_id": trade_id, "setup": p.setup, "score": int(p.score), "day": today_utc()})
        save_state()

        sent = await tg_send(msg_entry(p))
        return {"ok": True, "status": "active_set", "asset": asset, "telegram": sent}

    # -------------------------
    # RESOLVE
    # -------------------------
    if ev == "RESOLVE":
        active = state["active"].get(asset)
        if not active:
            hist({"ts": utc_now_iso(), "type": "RESOLVE_NO_ACTIVE", "asset": asset, "trade_id": trade_id})
            return {"ok": True, "ignored": True, "reason": "no_active_trade"}

        if str(active.get("trade_id")) != str(trade_id):
            hist({"ts": utc_now_iso(), "type": "RESOLVE_MISMATCH", "asset": asset, "incoming": trade_id, "active": active.get("trade_id")})
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        result = (p.result or "").strip().upper()
        setup = active.get("setup", p.setup or "CORE")

        record_setup(asset, setup, result)

        state["active"].pop(asset, None)
        hist({"ts": utc_now_iso(), "type": "RESOLVE", "asset": asset, "trade_id": trade_id, "result": result, "setup": setup, "day": today_utc()})
        save_state()

        sent = await tg_send(msg_resolve(asset, trade_id, result, setup))
        return {"ok": True, "status": "closed", "asset": asset, "result": result, "telegram": sent}

    hist({"ts": utc_now_iso(), "type": "UNKNOWN_EVENT", "asset": asset, "event": ev})
    return {"ok": True, "ignored": True, "reason": "unknown_event"}
