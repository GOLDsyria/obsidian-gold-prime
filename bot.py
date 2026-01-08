import os
import time
import json
import random
import hashlib
import logging
import requests
import importlib
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from tradingview_ta import TA_Handler, Interval

# ===================== 🛡️ نظام التخفي ومنع الحظر (Anti-Ban System) 🛡️ =====================
# إعادة تحميل مكتبة requests لضمان نظافة الجلسة
importlib.reload(requests)

# قائمة بصمات متصفحات حقيقية لخداع السيرفر
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/120.0.6099.119 Mobile/15E148 Safari/604.1"
]

# نحتفظ بالدالة الأصلية للطلبات
_real_post = requests.post

def patched_post(url, **kwargs):
    """
    دالة وسيطة تعترض الطلبات:
    1. إذا كانت لتيليجرام: تمررها فوراً.
    2. إذا كانت لـ TradingView: تقوم بتغيير الهوية (User-Agent) قبل الإرسال.
    """
    # استثناء تيليجرام من التمويه لتجنب المشاكل
    if "api.telegram.org" in url:
        return _real_post(url, **kwargs)

    # تمويه الطلب ليبدو وكأنه من متصفح حقيقي
    headers = kwargs.get('headers', {})
    headers['User-Agent'] = random.choice(USER_AGENTS)
    headers['Referer'] = 'https://www.tradingview.com/'
    headers['Origin'] = 'https://www.tradingview.com'
    headers['Accept-Language'] = 'en-US,en;q=0.9'
    
    kwargs['headers'] = headers
    
    # ضمان وجود مهلة زمنية
    if 'timeout' not in kwargs:
        kwargs['timeout'] = 15

    return _real_post(url, **kwargs)

# تطبيق التعديل على المكتبة
requests.post = patched_post
# =====================================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BOT_NAME = os.getenv("BOT_NAME", "الشاهين").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# الافتراضي: ذهب/فضة من OANDA، نفط من TVC
ASSETS_JSON = os.getenv(
    "ASSETS_JSON",
    json.dumps([
        {"name":"XAUUSD","screener":"forex","exchange":"OANDA","symbol":"XAUUSD"},
        {"name":"XAGUSD","screener":"forex","exchange":"OANDA","symbol":"XAGUSD"},
        {"name":"USOIL","screener":"cfd","exchange":"TVC","symbol":"USOIL"},
    ])
)

PRIMARY_TF = os.getenv("PRIMARY_TF", "5m")
CONFIRM_TF = os.getenv("CONFIRM_TF", "15m")
CONFIRM_REQUIRED = os.getenv("CONFIRM_REQUIRED", "true").lower() == "true"

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
JITTER_SECONDS = float(os.getenv("JITTER_SECONDS", "2.0"))
MIN_GAP_PER_ASSET_SEC = float(os.getenv("MIN_GAP_PER_ASSET_SEC", "20"))

MIN_SCORE = float(os.getenv("MIN_SCORE", "58"))

ATR_MULT_SL = float(os.getenv("ATR_MULT_SL", "1.0"))
RR_TP1 = float(os.getenv("RR_TP1", "0.6"))
RR_TP2 = float(os.getenv("RR_TP2", "1.0"))
RR_TP3 = float(os.getenv("RR_TP3", "1.4"))

def tv_interval(tf: str):
    m = {
        "1m": Interval.INTERVAL_1_MINUTE,
        "3m": Interval.INTERVAL_3_MINUTES,
        "5m": Interval.INTERVAL_5_MINUTES,
        "15m": Interval.INTERVAL_15_MINUTES,
        "30m": Interval.INTERVAL_30_MINUTES,
        "1h": Interval.INTERVAL_1_HOUR,
        "2h": Interval.INTERVAL_2_HOURS,
        "4h": Interval.INTERVAL_4_HOURS,
        "1d": Interval.INTERVAL_1_DAY,
    }
    return m.get(tf.strip().lower(), Interval.INTERVAL_5_MINUTES)

def must_env(name: str, val: str):
    if not val:
        raise RuntimeError(f"Missing env var: {name}")

def tg_send(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        # نستخدم الدالة الأصلية هنا عبر الـ patch الذي يستثني تيليجرام
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        logging.error("TG send failed: %s %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        logging.exception("TG exception: %s", e)
        return False

def rnd(x: float, n: int = 3) -> str:
    try:
        return f"{float(x):.{n}f}"
    except Exception:
        return str(x)

def hawk_banner():
    tg_send(f"{BOT_NAME} في الأجواء (نظام التخفي مفعل 🛡️) 🦅")

def hawk_catch(asset: str, side: str):
    ar = "شراء" if side == "BUY" else "بيع"
    return f"🦅 {BOT_NAME} التقط فريسة: {asset} ({ar})"

def hawk_eat(asset: str):
    return f"🦅 {BOT_NAME} التهم الفريسة ✅ ({asset})"

def hawk_escape(asset: str):
    return f"🦅 الفريسة هربت… سنبحث عن غيرها ❌ ({asset})"

@dataclass
class Signal:
    side: str
    score: float
    price: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    note: str

class State:
    def __init__(self):
        self.last_sent_at: Dict[str, float] = {}
        self.active: Dict[str, dict] = {}
        self.win_marked: Dict[str, bool] = {}
        self.last_msg_hash: Dict[str, str] = {}  # dedup per asset

STATE = State()

def can_send(asset: str) -> bool:
    return (time.time() - STATE.last_sent_at.get(asset, 0.0)) >= MIN_GAP_PER_ASSET_SEC

def mark_sent(asset: str):
    STATE.last_sent_at[asset] = time.time()

def dedup_send(asset: str, msg: str) -> bool:
    h = hashlib.sha256(msg.encode("utf-8")).hexdigest()[:16]
    if STATE.last_msg_hash.get(asset) == h:
        return False
    STATE.last_msg_hash[asset] = h
    return tg_send(msg)

def fetch_analysis(screener: str, exchange: str, symbol: str, tf: str):
    # سيتم استخدام requests.post المعدلة تلقائياً هنا
    h = TA_Handler(symbol=symbol, exchange=exchange, screener=screener, interval=tv_interval(tf), timeout=20)
    return h.get_analysis()

def extract_ind(a) -> Dict[str, float]:
    ind = dict(a.indicators or {})
    if "close" not in ind and "Close" in ind:
        ind["close"] = ind["Close"]
    return ind

def summary_score(a) -> float:
    s = a.summary or {}
    buy = float(s.get("BUY", 0))
    sell = float(s.get("SELL", 0))
    neu = float(s.get("NEUTRAL", 0))
    total = max(buy + sell + neu, 1.0)
    bias = (buy - sell) / total
    return max(0.0, min(100.0, 50.0 + bias * 50.0))

def side_from_reco(a) -> Optional[str]:
    reco = ((a.summary or {}).get("RECOMMENDATION", "") or "").upper()
    if "BUY" in reco and "SELL" not in reco:
        return "BUY"
    if "SELL" in reco and "BUY" not in reco:
        return "SELL"
    return None

def compute_signal(ind: Dict[str, float]) -> Optional[Tuple[str, float, str]]:
    close = ind.get("close")
    ema20 = ind.get("EMA20")
    ema50 = ind.get("EMA50")
    rsi = ind.get("RSI")
    macd = ind.get("MACD.macd")
    macds = ind.get("MACD.signal")
    bbp = ind.get("BBP")
    stoch_k = ind.get("Stoch.K")
    stoch_d = ind.get("Stoch.D")
    if close is None or ema20 is None or ema50 is None or rsi is None:
        return None

    long_bias = ema20 > ema50
    short_bias = ema20 < ema50

    score = 50.0
    notes = []

    if long_bias:
        score += 8; notes.append("TrendUp")
    if short_bias:
        score += 8; notes.append("TrendDown")

    # RSI مرن
    if long_bias and rsi >= 47:
        score += 7; notes.append("RSI_OK")
    if short_bias and rsi <= 53:
        score += 7; notes.append("RSI_OK")

    if macd is not None and macds is not None:
        if long_bias and macd > macds:
            score += 9; notes.append("MACD_Bull")
        if short_bias and macd < macds:
            score += 9; notes.append("MACD_Bear")

    # Discount/Premium proxy (خفيف)
    if bbp is not None:
        if long_bias and bbp < 0.45:
            score += 9; notes.append("Discount")
        if short_bias and bbp > 0.55:
            score += 9; notes.append("Premium")

    if stoch_k is not None and stoch_d is not None:
        if long_bias and stoch_k < 45 and stoch_k > stoch_d:
            score += 7; notes.append("StochUp")
        if short_bias and stoch_k > 55 and stoch_k < stoch_d:
            score += 7; notes.append("StochDown")

    if long_bias and score >= 55:
        return ("BUY", min(score, 100.0), " | ".join(notes))
    if short_bias and score >= 55:
        return ("SELL", min(score, 100.0), " | ".join(notes))
    return None

def build_levels(price: float, atr: float, side: str):
    d = (atr if atr and atr > 0 else price * 0.0013) * ATR_MULT_SL
    if side == "BUY":
        sl = price - d
        tp1 = price + d * RR_TP1
        tp2 = price + d * RR_TP2
        tp3 = price + d * RR_TP3
    else:
        sl = price + d
        tp1 = price - d * RR_TP1
        tp2 = price - d * RR_TP2
        tp3 = price - d * RR_TP3
    return sl, tp1, tp2, tp3

def fmt_entry(asset: str, tf: str, sig: Signal) -> str:
    emoji = "🟢" if sig.side == "BUY" else "🔴"
    ar = "شراء" if sig.side == "BUY" else "بيع"
    return (
        f"🜂 {BOT_NAME}\n"
        f"{emoji} {ar}\n"
        f"الرمز: {asset}\n"
        f"الإطار: {tf}\n"
        f"السعر: {rnd(sig.price)}\n"
        f"ستوب: {rnd(sig.sl)}\n"
        f"هدف ١: {rnd(sig.tp1)}\n"
        f"هدف ٢: {rnd(sig.tp2)}\n"
        f"هدف ٣: {rnd(sig.tp3)}\n"
        f"ملاحظة: ENTRY | Score={rnd(sig.score,1)} | {sig.note}"
    )

def fmt_update(asset: str, tf: str, side: str, price: float, sl: float, tp1: float, tp2: float, tp3: float, note: str) -> str:
    emoji = "🟢" if side == "BUY" else "🔴"
    ar = "شراء" if side == "BUY" else "بيع"
    return (
        f"🜂 {BOT_NAME}\n"
        f"{emoji} {ar}\n"
        f"الرمز: {asset}\n"
        f"الإطار: {tf}\n"
        f"السعر: {rnd(price)}\n"
        f"ستوب: {rnd(sl)}\n"
        f"هدف ١: {rnd(tp1)}\n"
        f"هدف ٢: {rnd(tp2)}\n"
        f"هدف ٣: {rnd(tp3)}\n"
        f"ملاحظة: {note}"
    )

def hit_tp(side: str, price: float, level: float) -> bool:
    return price >= level if side == "BUY" else price <= level

def hit_sl(side: str, price: float, level: float) -> bool:
    return price <= level if side == "BUY" else price >= level

def followups(asset: str, tf: str, price: float):
    tr = STATE.active.get(asset)
    if not tr:
        return
    side = tr["side"]
    sl = tr["sl"]; tp1 = tr["tp1"]; tp2 = tr["tp2"]; tp3 = tr["tp3"]

    if (not tr["tp1_sent"]) and hit_tp(side, price, tp1):
        tr["tp1_sent"] = True
        STATE.win_marked[asset] = True
        dedup_send(asset, hawk_eat(asset))
        dedup_send(asset, fmt_update(asset, tf, side, price, sl, tp1, tp2, tp3, "TP1_HIT -> TRADE NOW WIN ✅"))
        # move SL to BE
        tr["sl"] = tr["entry"]
        dedup_send(asset, fmt_update(asset, tf, side, price, tr["sl"], tp1, tp2, tp3, "SL_MOVED_BE"))

    if (not tr["tp2_sent"]) and hit_tp(side, price, tp2):
        tr["tp2_sent"] = True
        dedup_send(asset, fmt_update(asset, tf, side, price, tr["sl"], tp1, tp2, tp3, "TP2_HIT"))

    if (not tr["tp3_sent"]) and hit_tp(side, price, tp3):
        tr["tp3_sent"] = True
        dedup_send(asset, fmt_update(asset, tf, side, price, tr["sl"], tp1, tp2, tp3, "TP3_HIT"))
        dedup_send(asset, fmt_update(asset, tf, side, price, tr["sl"], tp1, tp2, tp3, "CLOSE_WIN ✅"))
        STATE.active.pop(asset, None)
        return

    if (not tr["sl_sent"]) and hit_sl(side, price, tr["sl"]):
        tr["sl_sent"] = True
        if STATE.win_marked.get(asset):
            dedup_send(asset, fmt_update(asset, tf, side, price, tr["sl"], tp1, tp2, tp3, "CLOSE_WIN (after TP1) ✅"))
        else:
            dedup_send(asset, hawk_escape(asset))
            dedup_send(asset, fmt_update(asset, tf, side, price, tr["sl"], tp1, tp2, tp3, "CLOSE_LOSS ❌"))
        STATE.active.pop(asset, None)

def main():
    must_env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
    must_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    assets = json.loads(ASSETS_JSON)
    hawk_banner()

    cursor = 0
    backoff = 0.0

    while True:
        try:
            a = assets[cursor % len(assets)]
            cursor += 1
            name = a["name"]; screener = a["screener"]; exchange = a["exchange"]; symbol = a["symbol"]

            an1 = fetch_analysis(screener, exchange, symbol, PRIMARY_TF)
            if an1 is None:
                # إذا حدث خطأ ما ولم يرجع التحليل
                time.sleep(1); continue

            ind1 = extract_ind(an1)
            price = ind1.get("close")
            if price is None:
                time.sleep(1); continue
            price = float(price)

            followups(name, PRIMARY_TF, price)

            if name in STATE.active:
                time.sleep(0.2)
                continue

            base = compute_signal(ind1)
            if not base:
                backoff = max(0.0, backoff - 0.25)
            else:
                side, smc_score, smc_note = base
                tvs = summary_score(an1)
                score = 0.6 * smc_score + 0.4 * tvs

                if CONFIRM_REQUIRED:
                    an2 = fetch_analysis(screener, exchange, symbol, CONFIRM_TF)
                    # قد يعود an2 بـ None في حالات الحظر الشديد، يجب التعامل معه
                    if an2:
                        side2 = side_from_reco(an2)
                        if side2 and side2 != side:
                            score -= 6
                            smc_note += " | TF15_conflict"
                        else:
                            score += 3
                            smc_note += " | TF15_ok"

                if score >= MIN_SCORE and can_send(name):
                    atr = float(ind1.get("ATR") or 0.0)
                    sl, tp1, tp2, tp3 = build_levels(price, atr, side)
                    sig = Signal(side=side, score=score, price=price, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, note=smc_note)

                    dedup_send(name, hawk_catch(name, side))
                    dedup_send(name, fmt_entry(name, PRIMARY_TF, sig))
                    mark_sent(name)

                    STATE.active[name] = {
                        "side": side,
                        "entry": price,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "tp1_sent": False,
                        "tp2_sent": False,
                        "tp3_sent": False,
                        "sl_sent": False,
                    }
                    STATE.win_marked[name] = False

            sleep_s = max(0.8, POLL_SECONDS + random.uniform(0, JITTER_SECONDS) + backoff)
            time.sleep(sleep_s)

        except Exception as e:
            logging.exception("Loop error: %s", e)
            # عند حدوث خطأ، نزيد فترة الانتظار قليلاً لتجنب الإصرار على الخطأ
            backoff = min(45.0, backoff * 1.5 + 2.0)
            time.sleep(5 + backoff)

if __name__ == "__main__":
    main()
