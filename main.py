import os
import asyncio
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import engine, Base, get_db
import models

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

Base.metadata.create_all(bind=engine)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aura-planner-ejyi.onrender.com")

app = FastAPI(title="Aura AI Planner API")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СХЕМЫ ДАННЫХ ---
class UserAuth(BaseModel):
    telegram_id: int
    language: Optional[str] = "ru"

class RecordCreate(BaseModel):
    telegram_id: int
    category: str
    title: str
    amount: Optional[float] = 0.0
    currency: Optional[str] = "$"

# --- МАРШРУТЫ API ---

@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="<h1>Ошибка: index.html не найден</h1>", status_code=404)

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

# --- ТЕЛЕГРАМ БОТ: ОБРАБОТКА ГОЛОСОВЫХ И ТЕКСТА ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Планировщик", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\nОтправляй мне **текстовые или голосовые сообщения**, и я автоматически добавлю их в планировщик!",
        reply_markup=markup
    )

# Функция автопарсинга текста по категориям
def parse_and_save(telegram_id: int, text: str):
    db = next(get_db())
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id)
        db.add(user)
        db.commit()

    category = "task"
    amount = 0.0

    import re
    numbers = re.findall(r'\d+', text)
    text_lower = text.lower()

    if numbers and any(k in text_lower for k in ["руб", "$", "драм", "֏", "купил", "потратил", "цена", "стоил"]):
        category = "finance"
        amount = float(numbers[0])
    elif "привычк" in text_lower or "каждый день" in text_lower:
        category = "habit"
        
    record = models.Record(
        user_id=user.id,
        category=category,
        title=text,
        amount=amount,
        currency="$"
    )
    db.add(record)
    db.commit()
    return category, amount

# Прием обычного текста в чате бота
@dp.message(F.text)
async def handle_text_message(message: types.Message):
    cat, amt = parse_and_save(message.from_user.id, message.text)
    emoji = "💸" if cat == "finance" else "✅" if cat == "task" else "🔄"
    await message.answer(f"{emoji} Записано в приложение: **{message.text}**")

# Прием голосовых сообщений в чате бота
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    await message.answer("🎙 Принял голосовое сообщение! Обрабатываю...")
    # Здесь можно подключить Whisper API / OpenAI API для идеальной расшифровки
    recognized_text = "Голосовая заметка: " + (message.caption or "Новая запись")
    cat, amt = parse_and_save(message.from_user.id, recognized_text)
    await message.answer(f"✅ Добавлено в планер!")

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))