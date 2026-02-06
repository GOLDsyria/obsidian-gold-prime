from fastapi import FastAPI
from app.server import router

app = FastAPI(title="Gold Scalping Bot")

app.include_router(router)
