from fastapi import HTTPException
from app.config import TV_WEBHOOK_SECRET
from app.telegram import send_message
from app.utils import format_signal

def handle_webhook(payload: dict):
    if payload.get("secret") != TV_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    required = ["symbol", "timeframe", "direction", "price"]
    for r in required:
        if r not in payload:
            raise HTTPException(status_code=400, detail=f"Missing {r}")

    message = format_signal(payload)
    send_message(message)

    return {"status": "ok"}
