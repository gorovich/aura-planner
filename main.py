import os
import asyncio
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import engine, Base, get_db
import models

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

# Автоматически создаем таблицы в базе данных SQL
Base.metadata.create_all(bind=engine)

# Переменные окружения с сервера Render
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aura-planner-ejyi.onrender.com")

app = FastAPI(title="Aura AI Planner API")

# Безопасное монтирование статики
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Инициализация Telegram бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СХЕМЫ ДАННЫХ (Pydantic) ---
class UserAuth(BaseModel):
    telegram_id: int
    language: Optional[str] = "ru"

class RecordCreate(BaseModel):
    telegram_id: int
    category: str
    title: str
    amount: Optional[float] = 0.0
    currency: Optional[str] = "$"

# --- МАРШРУТЫ (API & FRONTEND) ---

@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="<h1>Ошибка: Файл index.html не найден в папке static</h1>", status_code=404)

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
    records = db.query(models.Record).filter(models.Record.user_id == user.id).all()
    return records

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
        raise HTTPException(status_code=404, detail="Запись не найдена")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}

# --- ХЕНДЛЕРЫ TELEGRAM БОТА ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Планировщик", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\nНажми кнопку ниже, чтобы открыть планировщик.",
        reply_markup=markup
    )

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))