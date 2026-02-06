from fastapi import APIRouter, Request
from app.webhook import handle_webhook

router = APIRouter()

@router.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    return handle_webhook(payload)
