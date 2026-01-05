# bot.py
# -*- coding: utf-8 -*-

import os
import json
import time
import hmac
import hashlib
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx


# =========================
# Environment (Koyeb)
# =========================
BOT_NAME = os.getenv("BOT_NAME", "OBSIDIAN GOLD PRIME")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # MUST set in Koyeb

# Optional (not required for webhook to work)
REPORT_EVERY_MIN = int(os.getenv("REPORT_EVERY_MIN", "180"))

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not WEBHOOK_SECRET:
    # We don't crash the app (Koyeb will keep restarting); but we will block /tv with clear errors.
    pass

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage" if TELEGRAM_TOKEN else ""


# =========================
# FastAPI app
# =========================
app = FastAPI(title="TradingView Webhook Relay", version="1.0.0")

_last_report_ts = 0.0


# =========================
# Helpers
# =========================
def _safe_str(x: Any) -> str:
    try:
        return str(x)
    except Exception:
        return ""


def _timed_equal(a: str, b: str) -> bool:
    # constant-time compare
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _extract_secret_and_payload(obj: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Accept multiple key names for secret to make it easy:
    secret / token / passphrase / webhook_secret
    """
    for k in ("secret", "token", "passphrase", "webhook_secret"):
        if k in obj and obj[k] is not None:
            sec = _safe_str(obj[k]).strip()
            # remove secret from payload copy
            payload = dict(obj)
            payload.pop(k, None)
            return sec, payload
    return None, obj


def _parse_kv_text(text: str) -> Dict[str, Any]:
    """
    Supports text like:
    secret=XXXX|side=BUY|symbol=XAUUSD|price=2345.6
    or
    secret:XXXX; side:SELL; message:....
    """
    out: Dict[str, Any] = {"raw": text}
    # normalize separators
    normalized = text.replace("\n", "|").replace(";", "|").replace(",", "|")
    parts = [p.strip() for p in normalized.split("|") if p.strip()]
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
        elif ":" in p:
            k, v = p.split(":", 1)
        else:
            continue
        k = k.strip().lower()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _format_telegram_message(payload: Dict[str, Any]) -> str:
    """
    Build a readable Telegram message.
    Works with different payload styles.
    """
    # Common fields (if you send them from Pine)
    side = payload.get("side") or payload.get("signal") or payload.get("action")
    symbol = payload.get("symbol") or payload.get("ticker") or payload.get("s") or payload.get("tv_symbol")
    tf = payload.get("tf") or payload.get("timeframe") or payload.get("interval")
    price = payload.get("price") or payload.get("p")
    sl = payload.get("sl") or payload.get("stop") or payload.get("stoploss")
    tp1 = payload.get("tp1")
    tp2 = payload.get("tp2")
    tp3 = payload.get("tp3")
    note = payload.get("note") or payload.get("comment") or payload.get("msg") or payload.get("message")
    ts = payload.get("time") or payload.get("timestamp") or payload.get("t")

    # Fallback: if TradingView sent raw text, preserve it.
    raw = payload.get("raw")
    if not (side or symbol or price or sl or tp1 or tp2 or tp3 or note) and raw:
        return f"🤖 {BOT_NAME}\n\n📩 Webhook:\n{raw}"

    # Compose
    lines = [f"🤖 {BOT_NAME}"]
    if side:
        side_up = _safe_str(side).upper()
        emoji = "🟢" if "BUY" in side_up or "LONG" in side_up else ("🔴" if "SELL" in side_up or "SHORT" in side_up else "🟡")
        lines.append(f"{emoji} Signal: {_safe_str(side)}")
    if symbol:
        lines.append(f"💱 Symbol: {_safe_str(symbol)}")
    if tf:
        lines.append(f"⏱ TF: {_safe_str(tf)}")
    if price:
        lines.append(f"💰 Price: {_safe_str(price)}")
    if sl:
        lines.append(f"🛡 SL: {_safe_str(sl)}")
    tps = [tp for tp in (tp1, tp2, tp3) if tp is not None]
    if tps:
        lines.append("🎯 Targets: " + " | ".join(_safe_str(x) for x in tps))
    if ts:
        lines.append(f"🕒 Time: {_safe_str(ts)}")
    if note:
        lines.append(f"\n📝 Note: {_safe_str(note)}")

    return "\n".join(lines).strip()


async def _send_telegram(text: str) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_TOKEN is missing.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is missing.")

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TG_API, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram API failed: {r.status_code} {r.text}")


def _ensure_secret_ok(provided: Optional[str]) -> None:
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="WEBHOOK_SECRET is not set on server.")
    if not provided:
        raise HTTPException(status_code=401, detail="Missing secret in webhook payload.")
    if not _timed_equal(provided.strip(), WEBHOOK_SECRET.strip()):
        raise HTTPException(status_code=401, detail="Invalid secret.")


# =========================
# Routes
# =========================
@app.get("/health")
async def health():
    return {"ok": True, "service": BOT_NAME}


@app.post("/tv")
async def tv_webhook(request: Request):
    """
    Accepts:
    1) JSON body: {"secret":"...", "side":"BUY", "symbol":"XAUUSD", ...}
    2) Raw text body: "secret=...|side=BUY|symbol=XAUUSD|..."
    3) Form-encoded body (rare)
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Server env vars missing (TELEGRAM_TOKEN/TELEGRAM_CHAT_ID/WEBHOOK_SECRET).")

    content_type = (request.headers.get("content-type") or "").lower()

    # Try JSON
    data: Dict[str, Any] = {}
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", errors="ignore").strip()

    if "application/json" in content_type:
        try:
            data = json.loads(body_text) if body_text else {}
        except Exception:
            # fall back to text parsing
            data = _parse_kv_text(body_text)
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            data = dict(form)  # type: ignore
        except Exception:
            data = _parse_kv_text(body_text)
    else:
        # TradingView often sends raw text = "Message" exactly
        # If that text itself is JSON, parse it.
        if body_text.startswith("{") and body_text.endswith("}"):
            try:
                data = json.loads(body_text)
            except Exception:
                data = _parse_kv_text(body_text)
        else:
            data = _parse_kv_text(body_text)

    # Extract + verify secret
    provided_secret, payload = _extract_secret_and_payload(data)
    _ensure_secret_ok(provided_secret)

    # Build message and send
    msg = _format_telegram_message(payload)
    try:
        await _send_telegram(msg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to send Telegram message: {e}")

    return JSONResponse({"ok": True})


@app.get("/")
async def root():
    return {"ok": True, "hint": "Use /health or POST /tv"}


# Optional: lightweight periodic status report endpoint (not required)
@app.get("/status")
async def status():
    global _last_report_ts
    now = time.time()
    can_report = (now - _last_report_ts) >= (REPORT_EVERY_MIN * 60)
    return {
        "ok": True,
        "service": BOT_NAME,
        "report_every_min": REPORT_EVERY_MIN,
        "can_report_now": can_report
    }
