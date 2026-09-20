import os
import re
import io
import asyncio
from typing import Optional, List
from pydantic import BaseModel
import httpx

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

import speech_recognition as sr
from pydub import AudioSegment

from database import engine, Base, get_db
import models

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

Base.metadata.create_all(bind=engine)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aura-planner-ejyi.onrender.com")

app = FastAPI(title="Aura OS Planner API")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация бесплатного распознавателя речи
recognizer = sr.Recognizer()

# --- БЕСПЛАТНАЯ ФУНКЦИЯ РАСПОЗНАВАНИЯ РЕЧИ (БЕЗ OPENAI) ---
def recognize_speech_free(audio_bytes: bytes) -> str:
    try:
        # Конвертируем входящее аудио из OGG/WEBM/ANY в WAV
        audio_stream = io.BytesIO(audio_bytes)
        sound = AudioSegment.from_file(audio_stream)
        
        wav_stream = io.BytesIO()
        sound.export(wav_stream, format="wav")
        wav_stream.seek(0)

        with sr.AudioFile(wav_stream) as source:
            audio_data = recognizer.record(source)
            
            # 1. Пробуем распознать как армянский (hy-AM)
            try:
                text_hy = recognizer.recognize_google(audio_data, language="hy-AM")
                if text_hy and len(text_hy.strip()) > 0:
                    return text_hy
            except sr.UnknownValueError:
                pass
            
            # 2. Если не армянский — пробуем русский (ru-RU)
            try:
                text_ru = recognizer.recognize_google(audio_data, language="ru-RU")
                if text_ru and len(text_ru.strip()) > 0:
                    return text_ru
            except sr.UnknownValueError:
                pass

    except Exception as e:
        print(f"Error converting/recognizing audio: {e}")
        
    return ""

# --- SCHEMAS ---
class UserAuth(BaseModel):
    telegram_id: int
    language: Optional[str] = "ru"

class RecordCreate(BaseModel):
    telegram_id: int
    title: str
    category: Optional[str] = "task"
    amount: Optional[float] = 0.0

class ShortcutPayload(BaseModel):
    text: str

# --- PARSER ---
def parse_and_save(telegram_id: int, text: str, db: Session):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id)
        db.add(user)
        db.commit()

    category = "task"
    amount = 0.0
    text_lower = text.lower()

    num_words = {
        "հիսուն": 50, "տաս": 10, "քսան": 20, "երեսուն": 30, "քառասուն": 40,
        "հարյուր": 100, "հազար": 1000, "пятьдесят": 50, "сто": 100, "тысяча": 1000
    }

    numbers = re.findall(r'\d+', text)
    if numbers:
        amount = float(numbers[0])
    else:
        for word, val in num_words.items():
            if word in text_lower:
                amount = float(val)
                break

    finance_keywords = [
        "руб", "$", "драм", "֏", "купил", "потратил", "цена", "стоил", "кофе", "кофե",
        "dollar", "dolar", "դոլար", "դրամ", "ծախս", "գնեցի", "կոֆե", "սուրճ", "ստացա"
    ]

    if amount > 0 or any(k in text_lower for k in finance_keywords):
        category = "finance"
        if amount == 0.0:
            amount = 1.0

    record = models.Record(
        user_id=user.id,
        category=category,
        title=text,
        amount=amount,
        currency="$"
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record

# --- REST API ---

@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse(content="<h1>Ошибка: index.html не найден</h1>", status_code=404)

@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/api/records/{telegram_id}")
def get_records(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user:
        return []
    return db.query(models.Record).filter(models.Record.user_id == user.id).order_by(models.Record.id.desc()).all()

@app.post("/api/records")
def create_record(data: RecordCreate, db: Session = Depends(get_db)):
    record = parse_and_save(data.telegram_id, data.title, db)
    return {
        "status": "ok",
        "id": record.id,
        "title": record.title,
        "category": record.category,
        "amount": record.amount
    }

@app.delete("/api/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.Record).filter(models.Record.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}

# Прием и бесплатная расшифровка голосовых записей с веб-микрофона
@app.post("/api/voice")
async def handle_web_voice(
    telegram_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    audio_bytes = await file.read()
    recognized_text = recognize_speech_free(audio_bytes)
    
    if not recognized_text:
        recognized_text = "Голосовая запись"

    rec = parse_and_save(telegram_id, recognized_text, db)
    return {
        "status": "ok", 
        "id": rec.id, 
        "title": rec.title, 
        "category": rec.category,
        "amount": rec.amount
    }

# Вход для Быстрых команд iOS (Siri)
@app.post("/api/shortcut")
def handle_shortcut(id: int, payload: ShortcutPayload, db: Session = Depends(get_db)):
    record = parse_and_save(id, payload.text, db)
    return {
        "status": "ok",
        "category": record.category,
        "amount": record.amount,
        "text": record.title
    }

# --- БОТ ХЭНДЛЕРЫ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Aura OS Planner", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"🎙 Отправляй мне текстовые или голосовые сообщения прямо сюда!",
        reply_markup=markup
    )

@dp.message(Command("shortcut"))
async def cmd_shortcut(message: types.Message):
    user_id = message.from_user.id
    user_api_url = f"{WEBAPP_URL}/api/shortcut?id={user_id}"

    await message.answer(
        f"🎙 **Голосовой ввод через Siri на iPhone**\n\n"
        f"Ваша ссылка для Быстрой команды iOS:\n\n"
        f"`{user_api_url}`",
        parse_mode="Markdown"
    )

@dp.message(F.text)
async def handle_text_message(message: types.Message):
    db = next(get_db())
    rec = parse_and_save(message.from_user.id, message.text, db)
    emoji = "💸" if rec.category == "finance" else "✅"
    await message.answer(f"{emoji} Записано в **{rec.category.upper()}**: {rec.title}", parse_mode="Markdown")

# Бесплатная расшифровка голосовых сообщений из чата Telegram
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    db = next(get_db())
    
    file_info = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    
    recognized_text = recognize_speech_free(file_bytes.read())
    if not recognized_text:
        recognized_text = "Голосовая запись"

    rec = parse_and_save(message.from_user.id, recognized_text, db)
    emoji = "💸" if rec.category == "finance" else "✅"
    await message.answer(f"🎙 {emoji} **Распознано:** «{rec.title}»\nЗаписано в **{rec.category.upper()}**!", parse_mode="Markdown")

# --- KEEP ALIVE ---
async def keep_alive():
    await asyncio.sleep(30)
    ping_url = f"{WEBAPP_URL}/ping"
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(ping_url)
                print(f"Self-ping successful: status {response.status_code}")
            except Exception as e:
                print(f"Self-ping failed: {e}")
            await asyncio.sleep(600)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(keep_alive())