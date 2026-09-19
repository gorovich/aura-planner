import os
import asyncio
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import engine, Base, get_db
import models

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

Base.metadata.create_all(bind=engine)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ОТ_BOTFATHER")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-render-app.onrender.com")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class UserAuth(BaseModel):
    telegram_id: int
    language: Optional[str] = "ru"

class RecordCreate(BaseModel):
    telegram_id: int
    category: str
    title: str
    amount: Optional[float] = 0.0
    currency: Optional[str] = "$"

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/auth")
def authenticate_user(data: UserAuth, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    if not user:
        user = models.User(telegram_id=data.telegram_id, language=data.language)
        db.add(user)
        db.commit()
        db.refresh(user)
    return {"status": "ok", "user_id": user.id, "language": user.language}

@app.get("/api/records/{telegram_id}")
def get_records(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user:
        return []
    return db.query(models.Record).filter(models.Record.user_id == user.id).all()

@app.post("/api/records")
def create_record(data: RecordCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    if not user:
        user = models.User(telegram_id=data.telegram_id)
        db.add(user)
        db.commit()

    record = models.Record(
        user_id=user.id,
        category=data.category,
        title=data.title,
        amount=data.amount,
        currency=data.currency
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record

@app.delete("/api/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.Record).filter(models.Record.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Планировщик", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(f"Привет, {message.from_user.first_name}! 👋\nНажми кнопку ниже, чтобы открыть планер.", reply_markup=markup)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))