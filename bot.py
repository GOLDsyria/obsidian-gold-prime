import os
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from telegram import Bot


# =========================
# ENV
# =========================
BOT_NAME = os.getenv("BOT_NAME", "🜂 OBSIDIAN GOLD PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ChangeMe")

# Optional controls
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "0"))  # set to 70 مثلا لو بدك فلترة
ALLOW_ASSETS = os.getenv("ALLOW_ASSETS", "")            # "XAUUSD,XAGUSD,BTCUSDT" أو اتركه فارغ = الكل
RISK_PCT = float(os.getenv("RISK_PCT", "0.25"))         # 0.25% default
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "0"))  # لو 0 => لا يحسب حجم الصفقة

# News blackout windows (UTC) as JSON list:
# مثال:
# [{"start":"2026-01-03T12:25:00Z","end":"2026-01-03T12:40:00Z","title":"NFP"}]
NEWS_BLACKOUTS_JSON = os.getenv("NEWS_BLACKOUTS", "[]")

STATE_FILE = os.getenv("STATE_FILE", "state.json")


# =========================
# DATA
# =========================
@dataclass
class Trade:
    trade_id: str
    asset: str
    exchange: str
    direction: str  # BUY/SELL
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    bias_15m: str
    confidence: int
    session: str
    status: str  # ACTIVE/WIN/LOSS
    opened_at_utc: str

    # optional derived
    risk_usd: float = 0.0
    rr_to_tp1: float = 0.0
    position_size_units: float = 0.0  # تقديري (units) إن توفر حساب

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
    last_update_utc: str = ""


# =========================
# UTILS
# =========================
def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _parse_iso_z(s: str) -> datetime:
    # يقبل ...Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

def load_blackouts() -> List[dict]:
    try:
        data = json.loads(NEWS_BLACKOUTS_JSON or "[]")
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def in_blackout(now: datetime, blackouts: List[dict]) -> Tuple[bool, str]:
    for b in blackouts:
        try:
            start = _parse_iso_z(b["start"])
            end = _parse_iso_z(b["end"])
            title = b.get("title", "NEWS")
            if start <= now <= end:
                return True, title
        except Exception:
            continue
    return False, ""


# =========================
# IO
# =========================
def load_state() -> State:
    if not os.path.exists(STATE_FILE):
        return State(active_trades={}, perf=Performance(), last_update_utc=_now_utc_iso())

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    active_raw = raw.get("active_trades", {}) or {}
    active_trades: Dict[str, Trade] = {}
    for k, v in active_raw.items():
        try:
            active_trades[k] = Trade(**v)
        except Exception:
            continue

    perf = Performance(**(raw.get("perf", {}) or {}))
    last_update_utc = raw.get("last_update_utc", _now_utc_iso())
    return State(active_trades=active_trades, perf=perf, last_update_utc=last_update_utc)

def save_state(state: State) -> None:
    raw = {
        "active_trades": {k: asdict(v) for k, v in state.active_trades.items()},
        "perf": asdict(state.perf),
        "last_update_utc": state.last_update_utc or _now_utc_iso(),
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        Bot(token=TELEGRAM_TOKEN).send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        # ما نوقف السيرفر بسبب تيليجرام
        print(f"[TG_ERR] {e}")

def calc_risk_and_size(entry: float, sl: float) -> Tuple[float, float]:
    """
    حساب تقديري:
    risk_usd = balance * (RISK_PCT / 100)
    size_units = risk_usd / abs(entry - sl)
    هذا مناسب كـ "unit sizing" عام. للفوركس/ذهب الحقيقي تحتاج pipValue/contract.
    """
    if ACCOUNT_BALANCE <= 0 or RISK_PCT <= 0:
        return 0.0, 0.0
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.0, 0.0
    risk_usd = ACCOUNT_BALANCE * (RISK_PCT / 100.0)
    size_units = risk_usd / dist
    return float(risk_usd), float(size_units)

def rr(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    reward = abs(tp - entry)
    return round(reward / risk, 2)

def format_signal(trade: Trade) -> str:
    size_line = ""
    if trade.risk_usd > 0 and trade.position_size_units > 0:
        size_line = f"\nRisk: ${trade.risk_usd:.2f} | Size(units): {trade.position_size_units:.4f}"

    return (
        f"{BOT_NAME}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 SMC / ICT / SK ENTRY\n\n"
        f"Asset: {trade.asset}\n"
        f"Exchange: {trade.exchange}\n"
        f"Direction: {trade.direction}\n"
        f"Confidence: {trade.confidence}/100\n"
        f"Session: {trade.session}\n"
        f"Bias(15m): {trade.bias_15m}\n"
        f"{size_line}\n\n"
        f"Entry: {trade.entry:.4f}\n"
        f"SL:    {trade.sl:.4f}\n"
        f"TP1:   {trade.tp1:.4f}  | RR≈ {trade.rr_to_tp1}\n"
        f"TP2:   {trade.tp2:.4f}\n"
        f"TP3:   {trade.tp3:.4f}\n\n"
        f"Trade ID: {trade.trade_id}\n"
        f"Status: ACTIVE"
    )

def format_update(trade: Trade, result: str) -> str:
    icon = "✅" if result == "WIN" else "❌"
    hype = "🔥 هدف تحقق! استمر بإدارة المخاطر." if result == "WIN" else "🧊 وقف ضرب. لا تطارد السوق."
    return (
        f"{BOT_NAME}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} TRADE CLOSED\n\n"
        f"Asset: {trade.asset}\n"
        f"Direction: {trade.direction}\n"
        f"Result: {result}\n"
        f"{hype}\n\n"
        f"Entry: {trade.entry:.4f} | SL: {trade.sl:.4f} | TP1: {trade.tp1:.4f}\n"
        f"Trade ID: {trade.trade_id}"
    )


# =========================
# WEBHOOK INPUT
# =========================
class TVPayloadLong(BaseModel):
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

# نسخة مفاتيح قصيرة لتجاوز حد 300 حرف في TradingView
# s=secret, e=event, id=trade_id, a=asset, x=exchange, d=direction
# en=entry, sl=sl, t1/t2/t3, b=bias_15m, c=confidence, se=session, r=result
class TVPayloadShort(BaseModel):
    s: str = Field(..., alias="s")
    e: str = Field(..., alias="e")
    id: str = Field(..., alias="id")
    a: str = Field(..., alias="a")
    x: str = Field(..., alias="x")
    d: str = Field(..., alias="d")
    en: float = Field(..., alias="en")
    sl: float = Field(..., alias="sl")
    t1: float = Field(..., alias="t1")
    t2: float = Field(..., alias="t2")
    t3: float = Field(..., alias="t3")
    b: str = Field(..., alias="b")
    c: int = Field(..., alias="c")
    se: str = Field(..., alias="se")
    r: Optional[str] = Field(default=None, alias="r")


app = FastAPI(title="OBSIDIAN GOLD PRIME")
STATE = load_state()
BLACKOUTS = load_blackouts()

def asset_allowed(asset: str) -> bool:
    if not ALLOW_ASSETS.strip():
        return True
    allowed = {x.strip().upper() for x in ALLOW_ASSETS.split(",") if x.strip()}
    return asset.upper() in allowed


@app.get("/health")
def health():
    return {"ok": True, "bot": BOT_NAME, "active_assets": len(STATE.active_trades)}

@app.get("/state")
def state_view():
    # عرض مبسط
    return {
        "ok": True,
        "active_trades": {k: asdict(v) for k, v in STATE.active_trades.items()},
        "perf": asdict(STATE.perf),
        "blackouts": BLACKOUTS,
        "last_update_utc": STATE.last_update_utc,
    }

class AdminSecret(BaseModel):
    secret: str

class AdminNotify(AdminSecret):
    text: str

@app.post("/admin/ping")
def admin_ping(body: AdminSecret):
    if body.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    tg_send("✅ Telegram connected successfully.")
    return {"ok": True, "telegram": "sent"}

@app.post("/admin/notify")
def admin_notify(body: AdminNotify):
    if body.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    tg_send(body.text)
    return {"ok": True, "telegram": "sent"}

@app.post("/admin/reset")
def admin_reset(body: AdminSecret):
    global STATE
    if body.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    STATE = State(active_trades={}, perf=Performance(), last_update_utc=_now_utc_iso())
    save_state(STATE)
    tg_send("♻️ State reset: cleared all active trades.")
    return {"ok": True, "reset": True}

class AdminBlackouts(AdminSecret):
    blackouts: List[dict]

@app.post("/admin/blackouts")
def admin_blackouts(body: AdminBlackouts):
    global BLACKOUTS
    if body.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    BLACKOUTS = body.blackouts
    tg_send(f"🧯 Updated blackouts: {len(BLACKOUTS)} window(s).")
    return {"ok": True, "count": len(BLACKOUTS)}

@app.post("/tv")
async def tv_webhook(request: Request):
    """
    يقبل payload طويل أو قصير
    """
    global STATE

    payload = await request.json()

    # حاول قصير أولاً
    is_short = "s" in payload and "e" in payload and "id" in payload
    if is_short:
        p = TVPayloadShort(**payload)
        secret = p.s
        event = p.e
        trade_id = p.id
        asset = p.a
        exchange = p.x
        direction = p.d
        entry = p.en
        sl = p.sl
        tp1, tp2, tp3 = p.t1, p.t2, p.t3
        bias_15m = p.b
        confidence = int(p.c)
        session = p.se
        result = p.r
    else:
        p = TVPayloadLong(**payload)
        secret = p.secret
        event = p.event
        trade_id = p.trade_id
        asset = p.asset
        exchange = p.exchange
        direction = p.direction
        entry = p.entry
        sl = p.sl
        tp1, tp2, tp3 = p.tp1, p.tp2, p.tp3
        bias_15m = p.bias_15m
        confidence = int(p.confidence)
        session = p.session
        result = p.result

    # حماية secret
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    asset_u = asset.upper()

    # فلترة أصل
    if not asset_allowed(asset_u):
        return {"ok": True, "ignored": True, "reason": "asset_not_allowed"}

    # فلترة ثقة
    if confidence < MIN_CONFIDENCE:
        return {"ok": True, "ignored": True, "reason": "low_confidence"}

    # فلترة أخبار (UTC)
    now = datetime.now(timezone.utc)
    blocked, title = in_blackout(now, BLACKOUTS)
    if blocked:
        return {"ok": True, "ignored": True, "reason": f"news_blackout:{title}"}

    event_u = str(event).upper().strip()

    # ENTRY
    if event_u == "ENTRY":
        if asset_u in STATE.active_trades:
            return {"ok": True, "ignored": True, "reason": "active_trade_exists"}

        risk_usd, size_units = calc_risk_and_size(entry, sl)
        trade = Trade(
            trade_id=trade_id,
            asset=asset_u,
            exchange=exchange,
            direction=direction.upper(),
            entry=float(entry),
            sl=float(sl),
            tp1=float(tp1),
            tp2=float(tp2),
            tp3=float(tp3),
            bias_15m=bias_15m,
            confidence=int(confidence),
            session=session,
            status="ACTIVE",
            opened_at_utc=_now_utc_iso(),
            risk_usd=risk_usd,
            rr_to_tp1=rr(entry, sl, tp1),
            position_size_units=size_units,
        )

        STATE.active_trades[asset_u] = trade
        STATE.last_update_utc = _now_utc_iso()
        save_state(STATE)
        tg_send(format_signal(trade))
        return {"ok": True, "active_set": True}

    # RESOLVE
    if event_u == "RESOLVE":
        if asset_u not in STATE.active_trades:
            return {"ok": True, "ignored": True, "reason": "no_active_trade"}

        active = STATE.active_trades[asset_u]

        if trade_id != active.trade_id:
            return {"ok": True, "ignored": True, "reason": "trade_id_mismatch"}

        res = (result or "").upper()
        if res not in ("WIN", "LOSS"):
            raise HTTPException(status_code=400, detail="Invalid result")

        # update perf
        STATE.perf.trades += 1
        if res == "WIN":
            STATE.perf.wins += 1
            STATE.perf.consec_losses = 0
        else:
            STATE.perf.losses += 1
            STATE.perf.consec_losses += 1

        active.status = res
        STATE.last_update_utc = _now_utc_iso()
        tg_send(format_update(active, res))

        del STATE.active_trades[asset_u]
        save_state(STATE)
        return {"ok": True, "closed": True}

    raise HTTPException(status_code=400, detail="Unknown event")
