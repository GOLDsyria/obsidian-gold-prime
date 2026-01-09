import os
import time
import json
import random
import logging
import requests
import importlib
import threading
from flask import Flask
from datetime import datetime
from dataclasses import dataclass
from tradingview_ta import TA_Handler, Interval, Exchange

# ===================== 🏥 FAKE SERVER FOR KOYEB 🏥 =====================
app = Flask('')

@app.route('/')
def home():
    return "Phantom Sniper (Gold & Silver Edition) is Running."

def run_http_server():
    app.run(host='0.0.0.0', port=8000)

# ===================== 🛡️ ANTI-BAN SYSTEM 🛡️ =====================
importlib.reload(requests)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/121.0.6167.138 Mobile/15E148 Safari/604.1"
]

_real_post = requests.post

def patched_post(url, **kwargs):
    if "api.telegram.org" in url:
        return _real_post(url, **kwargs)
    
    headers = kwargs.get('headers', {})
    headers['User-Agent'] = random.choice(USER_AGENTS)
    headers['Referer'] = 'https://www.tradingview.com/'
    if 'timeout' not in kwargs: kwargs['timeout'] = 10
    kwargs['headers'] = headers
    return _real_post(url, **kwargs)

requests.post = patched_post

# ===================== ⚙️ CONFIGURATION ⚙️ =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Phantom Sniper 👻")

# 🔥 تم حصر الأصول في الذهب والفضة فقط 🔥
ASSETS = [
    {"symbol": "XAUUSD", "screener": "forex", "exchange": "OANDA", "pip": 0.1, "digit": 2},
    {"symbol": "XAGUSD", "screener": "forex", "exchange": "OANDA", "pip": 0.01, "digit": 3},
]

TF_SCALP = Interval.INTERVAL_5_MINUTES
TF_TREND = Interval.INTERVAL_4_HOURS
MIN_SCORE = 70

@dataclass
class TradeSetup:
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    score: int
    reasons: list

# ===================== 🧠 THE PHANTOM ENGINE (MANAGER) 🧠 =====================

class PhantomEngine:
    def __init__(self):
        self.active_trades = {} 

    def send_tg(self, msg):
        if not TELEGRAM_TOKEN: return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"})
        except Exception as e:
            logging.error(f"Telegram Error: {e}")

    def get_data(self, asset, interval):
        try:
            handler = TA_Handler(
                symbol=asset['symbol'],
                exchange=asset['exchange'],
                screener=asset['screener'],
                interval=interval,
                timeout=10
            )
            return handler.get_analysis()
        except Exception:
            return None

    def calculate_score(self, asset, data_scalp, data_trend):
        score = 0
        reasons = []
        side = None
        
        # 1. Trend Bias (4H)
        trend_ema200 = data_trend.indicators.get('EMA200')
        trend_close = data_trend.indicators.get('close')
        
        if not (trend_close and trend_ema200): return None
        
        bias = "BUY" if trend_close > trend_ema200 else "SELL"
        
        # 2. Scalp Logic (5M)
        close = data_scalp.indicators['close']
        rsi = data_scalp.indicators.get('RSI', 50)
        p_s1 = data_scalp.indicators.get('Pivot.M.Classic.S1')
        p_r1 = data_scalp.indicators.get('Pivot.M.Classic.R1')
        ema10 = data_scalp.indicators.get('EMA10')
        ema50 = data_scalp.indicators.get('EMA50')

        if bias == "BUY":
            if ema10 and ema50 and ema10 > ema50: score += 25; reasons.append("EMA Alignment ✅")
            if rsi < 60 and rsi > 40: score += 25; reasons.append("RSI Momentum 🚀")
            elif rsi <= 35: score += 30; reasons.append("Oversold Bounce 💎")
            if p_s1 and abs(close - p_s1) / close < 0.002: score += 30; reasons.append("Liquidity Sweep S1 🧹")
            side = "BUY"

        elif bias == "SELL":
            if ema10 and ema50 and ema10 < ema50: score += 25; reasons.append("EMA Alignment ✅")
            if rsi > 40 and rsi < 60: score += 25; reasons.append("RSI Momentum 🔻")
            elif rsi >= 65: score += 30; reasons.append("Overbought Rejection 💎")
            if p_r1 and abs(close - p_r1) / close < 0.002: score += 30; reasons.append("Liquidity Sweep R1 🧹")
            side = "SELL"

        return TradeSetup(symbol=asset['symbol'], side=side, entry=close, sl=0, tp1=0, tp2=0, tp3=0, score=score, reasons=reasons)

    def calculate_targets(self, setup: TradeSetup, asset):
        pip = asset['pip']
        # تخصيص الستوب لكل معدن
        if asset['symbol'] == "XAUUSD":
            sl_pips = 35.0
        else: # XAGUSD
            sl_pips = 20.0
        
        sl_dist = sl_pips * pip
        tp1_dist = sl_dist * 1.0
        tp2_dist = sl_dist * 2.0
        tp3_dist = sl_dist * 3.5

        if setup.side == "BUY":
            setup.sl = setup.entry - sl_dist
            setup.tp1 = setup.entry + tp1_dist
            setup.tp2 = setup.entry + tp2_dist
            setup.tp3 = setup.entry + tp3_dist
        else:
            setup.sl = setup.entry + sl_dist
            setup.tp1 = setup.entry - tp1_dist
            setup.tp2 = setup.entry - tp2_dist
            setup.tp3 = setup.entry - tp3_dist
        return setup

    def monitor_trade(self, asset, current_price):
        symbol = asset['symbol']
        trade = self.active_trades[symbol]
        
        if trade['side'] == "BUY":
            pips = (current_price - trade['entry']) / asset['pip']
        else:
            pips = (trade['entry'] - current_price) / asset['pip']

        # Check SL
        sl_hit = (trade['side'] == "BUY" and current_price <= trade['sl']) or \
                 (trade['side'] == "SELL" and current_price >= trade['sl'])
        
        if sl_hit:
            msg = f"🛑 <b>SL HIT ({symbol})</b>\nPrice: {current_price}\nLoss: {pips:.1f} pips\n❌ Trade Closed."
            self.send_tg(msg)
            logging.info(f"{symbol} SL Hit. Removed from active.")
            del self.active_trades[symbol]
            return

        # Check TP1
        tp1_hit = (trade['side'] == "BUY" and current_price >= trade['tp1']) or \
                  (trade['side'] == "SELL" and current_price <= trade['tp1'])
        
        if tp1_hit and not trade['tp1_hit']:
            msg = f"✅ <b>TP1 HIT ({symbol})</b>\nPrice: {current_price}\nProfit: +{pips:.1f} pips\n🛡️ SL Moved to Entry (BE)."
            self.send_tg(msg)
            trade['tp1_hit'] = True
            trade['sl'] = trade['entry']

        # Check TP2
        tp2_hit = (trade['side'] == "BUY" and current_price >= trade['tp2']) or \
                  (trade['side'] == "SELL" and current_price <= trade['tp2'])

        if tp2_hit and not trade['tp2_hit']:
            msg = f"✅✅ <b>TP2 HIT ({symbol})</b>\nPrice: {current_price}\nProfit: +{pips:.1f} pips\n🔥 Great Move!"
            self.send_tg(msg)
            trade['tp2_hit'] = True

        # Check TP3
        tp3_hit = (trade['side'] == "BUY" and current_price >= trade['tp3']) or \
                  (trade['side'] == "SELL" and current_price <= trade['tp3'])

        if tp3_hit:
            msg = f"🏆 <b>TP3 HIT ({symbol})</b>\nPrice: {current_price}\nProfit: +{pips:.1f} pips\n💰 Trade Closed Fully."
            self.send_tg(msg)
            logging.info(f"{symbol} TP3 Hit. Removed from active.")
            del self.active_trades[symbol]
            return

    def run(self):
        logging.info(f"{BOT_NAME} Manager Started (Gold & Silver Only)...")
        t = threading.Thread(target=run_http_server)
        t.start()
        
        while True:
            for asset in ASSETS:
                try:
                    symbol = asset['symbol']
                    
                    # 1. جلب بيانات السكالبينغ (للسعر الحالي والتحليل)
                    data_scalp = self.get_data(asset, TF_SCALP)
                    if not data_scalp: continue
                    current_price = data_scalp.indicators['close']

                    # 2. إذا كانت هناك صفقة مفتوحة -> راقبها فقط
                    if symbol in self.active_trades:
                        logging.info(f"Monitoring active trade: {symbol} @ {current_price}")
                        self.monitor_trade(asset, current_price)
                        time.sleep(1)
                        continue 

                    # 3. إذا لم توجد صفقة -> ابحث عن فرصة
                    data_trend = self.get_data(asset, TF_TREND)
                    if not data_trend: continue
                    time.sleep(1)

                    setup = self.calculate_score(asset, data_scalp, data_trend)
                    
                    if setup and setup.score >= MIN_SCORE:
                        setup = self.calculate_targets(setup, asset)
                        
                        d = asset['digit']
                        msg = (
                            f"🚀 <b>{BOT_NAME} SIGNAL</b>\n"
                            f"💎 <b>{setup.symbol}</b> | {setup.side}\n"
                            f"💵 Entry: <code>{setup.entry:.{d}f}</code>\n"
                            f"🛑 SL: <code>{setup.sl:.{d}f}</code>\n"
                            f"🎯 TP1: <code>{setup.tp1:.{d}f}</code>\n"
                            f"🎯 TP2: <code>{setup.tp2:.{d}f}</code>\n"
                            f"🎯 TP3: <code>{setup.tp3:.{d}f}</code>\n"
                            f"📊 Score: {setup.score}/100"
                        )
                        self.send_tg(msg)
                        logging.info(f"OPENED TRADE: {symbol}")
                        
                        self.active_trades[symbol] = {
                            "side": setup.side,
                            "entry": setup.entry,
                            "sl": setup.sl,
                            "tp1": setup.tp1, "tp2": setup.tp2, "tp3": setup.tp3,
                            "tp1_hit": False, "tp2_hit": False
                        }
                    else:
                        logging.info(f"Scanning {symbol}: No Signal (Score {setup.score if setup else 0})")

                    time.sleep(2)

                except Exception as e:
                    logging.error(f"Loop Error ({asset.get('symbol')}): {e}")
            
            time.sleep(15)

if __name__ == "__main__":
    bot = PhantomEngine()
    bot.run()
