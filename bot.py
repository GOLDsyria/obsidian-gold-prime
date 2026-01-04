//@version=5
indicator("OBSIDIAN PRIME v4B • Balanced Scalping", overlay=true, max_labels_count=500)

// =====================
// Inputs (Balanced Preset)
// =====================
secret   = input.string("8f2c9b1a-ChangeMe", "Webhook Secret")
exchange = input.string("OANDA", "Exchange Tag")

minScore = input.int(72, "Min Score (Balanced)", minval=0, maxval=100)

useNewsBlock = input.bool(true, "News Block (manual window)")
newsSess     = input.session("1230-1330", "News Block Window")
newsBlocked  = useNewsBlock and not na(time(timeframe.period, newsSess))

// Metals session filter
useSessionMetals = input.bool(true, "Session Filter (Metals)")
sessMetals       = input.session("0800-1700", "London+NY (chart time)")
sessOK_metals    = not useSessionMetals or not na(time(timeframe.period, sessMetals))

// Cooldowns (minutes) - Balanced
coolWinMin  = input.int(5,  "Cooldown after WIN (min)", minval=0, maxval=240)
coolLossMin = input.int(12, "Cooldown after LOSS (min)", minval=0, maxval=240)

// Risk controls - Balanced
maxTradesPerSession = input.int(5, "Max Trades per Session", minval=1, maxval=30)
maxConsecLosses     = input.int(3, "Max Consecutive Losses", minval=1, maxval=8)

// Chop filter - Balanced
useChopFilter = input.bool(true, "Chop Filter")
atrLen        = input.int(14, "ATR Length", minval=5, maxval=50)
atrMinMult    = input.float(0.50, "ATR >= x * ATR(200) avg", minval=0.1, maxval=3.0)

// =====================
// Symbol routing
// =====================
t = str.upper(syminfo.ticker)
isXAU = str.contains(t, "XAU")
isXAG = str.contains(t, "XAG")
isBTC = str.contains(t, "BTC")
allowed = isXAU or isXAG or isBTC

sessOK = isBTC ? true : sessOK_metals

// =====================
// HTF Bias (15m)
// =====================
tfHTF = "15"
ema20 = request.security(syminfo.tickerid, tfHTF, ta.ema(close, 20), barmerge.gaps_off, barmerge.lookahead_off)
ema50 = request.security(syminfo.tickerid, tfHTF, ta.ema(close, 50), barmerge.gaps_off, barmerge.lookahead_off)
biasBull = ema20 > ema50
biasText = biasBull ? "Bullish" : "Bearish"

// =====================
// SMC / ICT: Liquidity sweep + PD
// =====================
len = 3
ph = ta.pivothigh(high, len, len)
pl = ta.pivotlow(low,  len, len)

var float lastHigh = na
var float lastLow  = na
if not na(ph)
    lastHigh := ph
if not na(pl)
    lastLow := pl

haveRange = not na(lastHigh) and not na(lastLow)
mid = haveRange ? (lastHigh + lastLow) / 2 : na

inDiscount = haveRange and close < mid
inPremium  = haveRange and close > mid

// Sweep (stop-hunt)
liqBuy  = haveRange and low  < lastLow  and close > lastLow
liqSell = haveRange and high > lastHigh and close < lastHigh

// =====================
// Intent / displacement + microtrend
// =====================
atr = ta.atr(atrLen)
disp = math.abs(close - open)
displacement = atr > 0 and disp > (0.75 * atr)   // slightly easier than v4

ema9  = ta.ema(close, 9)
ema21 = ta.ema(close, 21)
microBull = ema9 > ema21
microBear = ema9 < ema21

// Chop filter (relaxed)
atrSlow = ta.sma(ta.atr(200), 200)
chopOK = not useChopFilter or (atrSlow > 0 and atr >= atrMinMult * atrSlow)

// =====================
// “Balanced MSS” (Relaxed)
// Instead of requiring hard break, accept: sweep + (displacement OR microtrend)
// =====================
mssBuyRelax  = liqBuy  and (displacement or microBull)
mssSellRelax = liqSell and (displacement or microBear)

// =====================
// Setup classification (for learning)
// =====================
setup = "CORE"
setup := (displacement and (inDiscount or inPremium)) ? "IMPULSE" : setup
setup := (microBull or microBear) ? setup : "CORE"

// =====================
// Score (0-100) Balanced
// =====================
score = 0
score += allowed ? 20 : 0
score += (not newsBlocked) ? 10 : 0
score += sessOK ? 12 : 0
score += chopOK ? 8 : 0
score += (liqBuy or liqSell) ? 18 : 0
score += (mssBuyRelax or mssSellRelax) ? 18 : 0
score += (inDiscount or inPremium) ? 8 : 0
score += displacement ? 10 : 0
score += (microBull or microBear) ? 6 : 0
score := math.min(score, 100)

// =====================
// Targets per asset
// =====================
tp1Move = isXAU ? 4.5 : isXAG ? 0.45 : math.max(70.0, 0.35 * atr)
tp2Move = isXAU ? 8.0 : isXAG ? 0.80 : math.max(140.0, 0.70 * atr)
tp3Move = isXAU ? 12.0: isXAG ? 1.20 : math.max(240.0, 1.10 * atr)

slPadTicks = 10.0

// =====================
// Lifecycle
// =====================
var bool inTrade = false
var string tradeId = ""
var string dir = ""
var float entry = na
var float sl = na
var float tp1 = na
var float tp2 = na
var float tp3 = na

var int consecLosses = 0
var int tradesThisSession = 0
var int cooldownUntil = 0  // unix ms

// daily reset
isNewDay = ta.change(time("D")) != 0
if isNewDay
    tradesThisSession := 0
    consecLosses := 0

// metals session reset
if not isBTC and useSessionMetals and not sessOK
    tradesThisSession := 0

nowMs = time
cooldownActive = nowMs < cooldownUntil
riskStop = consecLosses >= maxConsecLosses
tradeLimitHit = tradesThisSession >= maxTradesPerSession

// =====================
// Entry (direction locked to 15m bias)
// =====================
gate = allowed and sessOK and (not newsBlocked) and chopOK and (not cooldownActive) and (not riskStop) and (not tradeLimitHit) and (score >= minScore)

longCond  = gate and biasBull and mssBuyRelax  and inDiscount and microBull
shortCond = gate and (not biasBull) and mssSellRelax and inPremium and microBear

newId() => str.tostring(time)

// =====================
// ENTRY
// =====================
if not inTrade and barstate.isconfirmed
    if longCond
        inTrade := true
        tradeId := newId()
        dir := "BUY"
        entry := close
        sl := low - syminfo.mintick * slPadTicks
        tp1 := entry + tp1Move
        tp2 := entry + tp2Move
        tp3 := entry + tp3Move

        alert(
         '{"s":"' + secret + '","e":"ENTRY","id":"' + tradeId +
         '","a":"' + syminfo.ticker + '","x":"' + exchange +
         '","d":"BUY","en":' + str.tostring(entry) +
         ',"sl":' + str.tostring(sl) +
         ',"t1":' + str.tostring(tp1) +
         ',"t2":' + str.tostring(tp2) +
         ',"t3":' + str.tostring(tp3) +
         ',"b":"' + biasText + '"' +
         ',"se":"' + (isBTC ? "CRYPTO" : "London+NY") + '"' +
         ',"st":"' + setup + '","sc":' + str.tostring(score) + ',"c":' + str.tostring(score) + '}',
         alert.freq_once_per_bar_close)

        tradesThisSession += 1

    else if shortCond
        inTrade := true
        tradeId := newId()
        dir := "SELL"
        entry := close
        sl := high + syminfo.mintick * slPadTicks
        tp1 := entry - tp1Move
        tp2 := entry - tp2Move
        tp3 := entry - tp3Move

        alert(
         '{"s":"' + secret + '","e":"ENTRY","id":"' + tradeId +
         '","a":"' + syminfo.ticker + '","x":"' + exchange +
         '","d":"SELL","en":' + str.tostring(entry) +
         ',"sl":' + str.tostring(sl) +
         ',"t1":' + str.tostring(tp1) +
         ',"t2":' + str.tostring(tp2) +
         ',"t3":' + str.tostring(tp3) +
         ',"b":"' + biasText + '"' +
         ',"se":"' + (isBTC ? "CRYPTO" : "London+NY") + '"' +
         ',"st":"' + setup + '","sc":' + str.tostring(score) + ',"c":' + str.tostring(score) + '}',
         alert.freq_once_per_bar_close)

        tradesThisSession += 1

// =====================
// RESOLVE (TP1 or SL)
// =====================
if inTrade
    hitSL  = dir == "BUY"  ? low <= sl   : high >= sl
    hitTP1 = dir == "BUY"  ? high >= tp1 : low  <= tp1

    if hitSL and barstate.isconfirmed
        consecLosses += 1
        cooldownUntil := nowMs + coolLossMin * 60 * 1000

        alert('{"s":"' + secret + '","e":"RESOLVE","id":"' + tradeId +
              '","a":"' + syminfo.ticker + '","r":"SL","st":"' + setup + '","sc":' + str.tostring(score) + '}',
              alert.freq_once_per_bar_close)

        inTrade := false

    else if hitTP1 and barstate.isconfirmed
        consecLosses := 0
        cooldownUntil := nowMs + coolWinMin * 60 * 1000

        alert('{"s":"' + secret + '","e":"RESOLVE","id":"' + tradeId +
              '","a":"' + syminfo.ticker + '","r":"TP1","st":"' + setup + '","sc":' + str.tostring(score) + '}',
              alert.freq_once_per_bar_close)

        inTrade := false

// =====================
// Visuals
// =====================
plotshape(longCond,  style=shape.triangleup,   location=location.belowbar, size=size.tiny, text="BUY")
plotshape(shortCond, style=shape.triangledown, location=location.abovebar, size=size.tiny, text="SELL")
plotchar(newsBlocked, char="⛔", title="News Block", location=location.top)
