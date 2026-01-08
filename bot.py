import os, time, random, threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI

# ===================== ENV
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "").strip()
OANDA_ENV        = os.getenv("OANDA_ENV", "practice").strip().lower()

BOT_NAME         = os.getenv("BOT_NAME", "الشاهين").strip()
INSTRUMENTS      = [x.strip() for x in os.getenv("INSTRUMENTS", "XAU_USD,XAG_USD,WTICO_USD").split(",") if x.strip()]
POLL_SEC         = float(os.getenv("POLL_SEC", "10").strip())

AGGRESSIVE       = os.getenv("AGGRESSIVE", "1").strip() in ("1", "true", "True", "yes", "YES")
MIN_SCORE        = int(os.getenv("MIN_SCORE", "55").strip())   # 50-60 balanced
COOLDOWN_MIN     = int(os.getenv("COOLDOWN_MIN", "10").strip())

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit("Missing TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
    raise SystemExit("Missing OANDA_API_KEY / OANDA_ACCOUNT_ID")

OANDA_BASE = "https://api-fxpractice.oanda.com" if OANDA_ENV == "practice" else "https://api-fxtrade.oanda.com"

# ===================== FASTAPI
app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "bot": BOT_NAME, "instruments": INSTRUMENTS, "version": "7.0.0"}

@app.get("/health")
def health():
    return {"ok": True, "status": "healthy", "version": "7.0.0"}


# ===================== TELEGRAM
def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram exception:", repr(e))

def hawk_msg_start():
    tg_send(f"🦅 {BOT_NAME} في الأجواء…\nجاهز لصيد الفرص (ذهب/فضة/نفط).")

def hawk_msg_entry(side: str, sym: str, tf: str, entry: float, sl: float, tp1: float, tp2: float, tp3: float, note: str):
    emo = "🟢 شراء" if side == "BUY" else "🔴 بيع"
    tg_send(
        f"🦅 {BOT_NAME} التقط فريسة!\n"
        f"{emo}\n"
        f"الرمز: {sym}\n"
        f"الإطار: {tf}\n"
        f"السعر: {fmt(entry)}\n"
        f"ستوب: {fmt(sl)}\n"
        f"هدف ١: {fmt(tp1)}\n"
        f"هدف ٢: {fmt(tp2)}\n"
        f"هدف ٣: {fmt(tp3)}\n"
        f"ملاحظة: {note}"
    )

def hawk_msg_tp1(side: str, sym: str, price: float):
    emo = "🟢 شراء" if side == "BUY" else "🔴 بيع"
    tg_send(f"🦅 {BOT_NAME} التهم الفريسة ✅ (TP1)\n{emo}\n{sym}\nسعر الآن: {fmt(price)}\nمن الآن الصفقة تُعدّ رابحة.")

def hawk_msg_tpN(side: str, sym: str, price: float, n: int):
    emo = "🟢 شراء" if side == "BUY" else "🔴 بيع"
    tg_send(f"🦅 متابعة الصيد (TP{n})\n{emo}\n{sym}\nسعر الآن: {fmt(price)}")

def hawk_msg_sl(side: str, sym: str, price: float):
    emo = "🟢 شراء" if side == "BUY" else "🔴 بيع"
    tg_send(f"🦅 الفريسة هربت… سنبحث عن غيرها 🥀\n{emo}\n{sym}\nسعر الآن: {fmt(price)}")

def hawk_msg_close(side: str, sym: str, price: float, win: bool, reason: str):
    emo = "🟢 شراء" if side == "BUY" else "🔴 بيع"
    badge = "🏆 ربح" if win else "🧨 خسارة"
    tg_send(f"🦅 إغلاق الصفقة: {badge}\n{emo}\n{sym}\nسعر الإغلاق: {fmt(price)}\nسبب: {reason}")


# ===================== UTILS
def fmt(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")

def jitter_sleep(sec: float):
    time.sleep(sec + random.uniform(0, 0.35))

def oanda_price_mid(instrument: str) -> float:
    url = f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    params = {"instruments": instrument}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"OANDA pricing error {r.status_code}: {r.text[:200]}")
    data = r.json()
    p = data["prices"][0]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    return (bid + ask) / 2.0

# ===================== CANDLES
@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float

class BarBuilder:
    def __init__(self, tf_sec: int):
        self.tf = tf_sec
        self.cur: Optional[Candle] = None
        self.bars: List[Candle] = []

    def _bucket(self, ts: int) -> int:
        return ts - (ts % self.tf)

    def update(self, ts: int, price: float):
        b = self._bucket(ts)
        if self.cur is None:
            self.cur = Candle(b, price, price, price, price)
            return
        if self.cur.t == b:
            self.cur.h = max(self.cur.h, price)
            self.cur.l = min(self.cur.l, price)
            self.cur.c = price
        else:
            self.bars.append(self.cur)
            self.cur = Candle(b, price, price, price, price)

    def series(self, n: int) -> List[Candle]:
        arr = self.bars[:]
        if self.cur:
            arr.append(self.cur)
        return arr[-n:]


# ===================== SMC/ICT ENGINE (Balanced Strong)
def atr(bars: List[Candle], length: int = 14) -> float:
    if len(bars) < length + 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        hi, lo = bars[i].h, bars[i].l
        pc = bars[i-1].c
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        trs.append(tr)
    w = trs[-length:]
    return sum(w) / len(w) if w else 0.0

def pivot_high(b: List[Candle], i: int, k: int) -> bool:
    if i-k < 0 or i+k >= len(b): return False
    x = b[i].h
    for j in range(i-k, i+k+1):
        if j != i and b[j].h >= x: return False
    return True

def pivot_low(b: List[Candle], i: int, k: int) -> bool:
    if i-k < 0 or i+k >= len(b): return False
    x = b[i].l
    for j in range(i-k, i+k+1):
        if j != i and b[j].l <= x: return False
    return True

def last_swings(b: List[Candle], k: int) -> Tuple[Optional[float], Optional[float]]:
    sh = sl = None
    for i in range(len(b)-3, 1, -1):  # avoid forming bar
        if sh is None and pivot_high(b, i, k): sh = b[i].h
        if sl is None and pivot_low(b, i, k):  sl = b[i].l
        if sh is not None and sl is not None: break
    return sh, sl

def liquidity_sweep(b: List[Candle], lookback: int = 20) -> Tuple[bool, bool]:
    if len(b) < lookback + 3: return False, False
    last = b[-2]
    w = b[-(lookback+2):-2]
    hh = max(x.h for x in w)
    ll = min(x.l for x in w)
    sweep_high = (last.h > hh) and (last.c < last.o)  # wick above + bearish close
    sweep_low  = (last.l < ll) and (last.c > last.o)  # wick below + bullish close
    return sweep_high, sweep_low

def bos_choch(b: List[Candle], k: int = 3) -> Tuple[bool, bool]:
    if len(b) < 30: return False, False
    sh, sl = last_swings(b, k)
    lc = b[-2].c
    bos_up = (sh is not None) and (lc > sh)
    bos_dn = (sl is not None) and (lc < sl)
    return bos_up, bos_dn

def fvg_ok(b: List[Candle], side: str) -> bool:
    # FVG بسيط (3 شموع مغلقة) لتأكيد الدفع
    if len(b) < 5: return True
    a, c = b[-4], b[-2]
    if side == "BUY":
        return c.l > a.h
    else:
        return c.h < a.l

def trend_bias_htf(m5: List[Candle], m15: List[Candle]) -> int:
    # باياس بسيط من هيكل أعلى: BOS على 15 + اتجاه آخر قاع/قمة
    up15, dn15 = bos_choch(m15, 3)
    up5, dn5 = bos_choch(m5, 3)
    if up15 and not dn15: return 1
    if dn15 and not up15: return -1
    # fallback: 5m
    if up5 and not dn5: return 1
    if dn5 and not up5: return -1
    return 0

def score_signal(m1: List[Candle], m5: List[Candle], m15: List[Candle]) -> Tuple[int, str, Optional[str]]:
    # يرجع (score, reason, side)
    if len(m1) < 80 or len(m5) < 40 or len(m15) < 30:
        return 0, "Not enough data", None

    sweepH, sweepL = liquidity_sweep(m5, 20)
    bosU, bosD = bos_choch(m5, 3)
    bias = trend_bias_htf(m5, m15)

    # نختار side مبدئيًا: sweep أهم من bos
    side = None
    if sweepL and not sweepH: side = "BUY"
    elif sweepH and not sweepL: side = "SELL"
    elif bosU and not bosD: side = "BUY"
    elif bosD and not bosU: side = "SELL"
    else:
        return 0, "No sweep/BOS", None

    score = 0
    reasons = []

    # قواعد SMC/ICT
    if side == "BUY" and sweepL: score += 25; reasons.append("SweepLow")
    if side == "SELL" and sweepH: score += 25; reasons.append("SweepHigh")

    if side == "BUY" and bosU: score += 20; reasons.append("BOS_UP")
    if side == "SELL" and bosD: score += 20; reasons.append("BOS_DN")

    # FVG
    if fvg_ok(m5, side): score += 15; reasons.append("FVG_OK")
    else:
        if not AGGRESSIVE:
            score -= 10; reasons.append("FVG_weak")

    # HTF bias
    if bias == (1 if side == "BUY" else -1):
        score += 15; reasons.append("HTF_BIAS")
    elif bias != 0:
        score -= (5 if AGGRESSIVE else 15); reasons.append("Against_HTF")

    # مومنتوم بسيط من 1m: جسم آخر شمعة
    last1 = m1[-2]
    body = abs(last1.c - last1.o)
    rng  = max(1e-9, (last1.h - last1.l))
    body_ratio = body / rng
    if body_ratio > 0.55:
        score += 10; reasons.append("Impulse1m")

    # لا نخنق كثير: إذا AGGRESSIVE نسمح أكثر
    return score, " | ".join(reasons), side


# ===================== TRADING STATE
@dataclass
class Trade:
    trade_id: str
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    opened_ts: int
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    closed: bool = False
    last_notify_ts: int = 0

class Engine:
    def __init__(self):
        self.builders: Dict[str, Dict[str, BarBuilder]] = {}
        self.trades: Dict[str, Optional[Trade]] = {s: None for s in INSTRUMENTS}
        self.cooldown_until: Dict[str, int] = {s: 0 for s in INSTRUMENTS}

        for s in INSTRUMENTS:
            self.builders[s] = {
                "1m": BarBuilder(60),
                "5m": BarBuilder(300),
                "15m": BarBuilder(900),
            }

    def update_price(self, sym: str, ts: int, price: float):
        self.builders[sym]["1m"].update(ts, price)
        self.builders[sym]["5m"].update(ts, price)
        self.builders[sym]["15m"].update(ts, price)

    def maybe_open(self, sym: str, price: float):
        # لا تفتح إذا في صفقة مفتوحة أو ضمن cooldown
        if self.trades[sym] is not None and not self.trades[sym].closed:
            return
        if int(time.time()) < self.cooldown_until[sym]:
            return

        m1 = self.builders[sym]["1m"].series(400)
        m5 = self.builders[sym]["5m"].series(200)
        m15= self.builders[sym]["15m"].series(120)

        score, reason, side = score_signal(m1, m5, m15)
        if side is None:
            return
        if score < MIN_SCORE:
            return

        a = atr(m5, 14)
        if a <= 0:
            return

        # Risk model (مناسب للذهب/فضة/نفط): ATR على 5m
        entry = m5[-2].c  # آخر شمعة 5m مغلقة
        risk  = a * 1.0

        if side == "BUY":
            sl = entry - risk
            tp1 = entry + risk * 0.6
            tp2 = entry + risk * 1.0
            tp3 = entry + risk * 1.5
        else:
            sl = entry + risk
            tp1 = entry - risk * 0.6
            tp2 = entry - risk * 1.0
            tp3 = entry - risk * 1.5

        tr = Trade(
            trade_id=str(m5[-2].t),
            symbol=sym,
            side=side,
            entry=entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            opened_ts=int(time.time())
        )
        self.trades[sym] = tr
        self.cooldown_until[sym] = int(time.time()) + COOLDOWN_MIN * 60

        hawk_msg_entry(side, sym.replace("_",""), "5m", entry, sl, tp1, tp2, tp3, f"SMC/ICT SCORE={score} | {reason}")

    def track_trade(self, sym: str, price: float):
        tr = self.trades.get(sym)
        if tr is None or tr.closed:
            return

        # منع spam: لا تبعث تحديثات متقاربة جدًا
        now = int(time.time())
        if now - tr.last_notify_ts < 3:
            return

        if tr.side == "BUY":
            if (not tr.tp1_hit) and price >= tr.tp1:
                tr.tp1_hit = True
                tr.last_notify_ts = now
                hawk_msg_tp1(tr.side, sym.replace("_",""), price)

            if (not tr.tp2_hit) and price >= tr.tp2:
                tr.tp2_hit = True
                tr.last_notify_ts = now
                hawk_msg_tpN(tr.side, sym.replace("_",""), price, 2)

            if (not tr.tp3_hit) and price >= tr.tp3:
                tr.tp3_hit = True
                tr.last_notify_ts = now
                hawk_msg_tpN(tr.side, sym.replace("_",""), price, 3)

            # SL
            if price <= tr.sl and not tr.closed:
                tr.closed = True
                tr.last_notify_ts = now
                if tr.tp1_hit:
                    hawk_msg_close(tr.side, sym.replace("_",""), price, True, "SL hit but TP1 already hit => WIN by rule")
                else:
                    hawk_msg_sl(tr.side, sym.replace("_",""), price)
                    hawk_msg_close(tr.side, sym.replace("_",""), price, False, "SL hit before TP1")

            # Close on TP3
            if tr.tp3_hit and not tr.closed:
                tr.closed = True
                tr.last_notify_ts = now
                hawk_msg_close(tr.side, sym.replace("_",""), price, True, "TP3 hit")

        else:
            if (not tr.tp1_hit) and price <= tr.tp1:
                tr.tp1_hit = True
                tr.last_notify_ts = now
                hawk_msg_tp1(tr.side, sym.replace("_",""), price)

            if (not tr.tp2_hit) and price <= tr.tp2:
                tr.tp2_hit = True
                tr.last_notify_ts = now
                hawk_msg_tpN(tr.side, sym.replace("_",""), price, 2)

            if (not tr.tp3_hit) and price <= tr.tp3:
                tr.tp3_hit = True
                tr.last_notify_ts = now
                hawk_msg_tpN(tr.side, sym.replace("_",""), price, 3)

            if price >= tr.sl and not tr.closed:
                tr.closed = True
                tr.last_notify_ts = now
                if tr.tp1_hit:
                    hawk_msg_close(tr.side, sym.replace("_",""), price, True, "SL hit but TP1 already hit => WIN by rule")
                else:
                    hawk_msg_sl(tr.side, sym.replace("_",""), price)
                    hawk_msg_close(tr.side, sym.replace("_",""), price, False, "SL hit before TP1")

            if tr.tp3_hit and not tr.closed:
                tr.closed = True
                tr.last_notify_ts = now
                hawk_msg_close(tr.side, sym.replace("_",""), price, True, "TP3 hit")


engine = Engine()


# ===================== BOT LOOP
def bot_loop():
    hawk_msg_start()

    last_5m_closed: Dict[str, int] = {s: -1 for s in INSTRUMENTS}

    while True:
        try:
            ts = int(time.time())

            for sym in INSTRUMENTS:
                price = oanda_price_mid(sym)
                engine.update_price(sym, ts, price)

                # متابعة الصفقة فورًا كل بول
                engine.track_trade(sym, price)

                # فتح صفقة فقط عند إغلاق شمعة 5m (حتى لا تتأخر بس تكون مؤكدة)
                m5 = engine.builders[sym]["5m"].series(5)
                if len(m5) >= 3:
                    closed_ts = m5[-2].t
                    if closed_ts != last_5m_closed[sym]:
                        last_5m_closed[sym] = closed_ts
                        engine.maybe_open(sym, price)

            jitter_sleep(POLL_SEC)

        except Exception as e:
            print("BOT_ERR:", repr(e))
            jitter_sleep(min(max(POLL_SEC * 2, 5), 30))


# ===================== START BACKGROUND THREAD
_started = False

@app.on_event("startup")
def startup_event():
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
