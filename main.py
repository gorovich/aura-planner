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
from sqlalchemy import text
from sqlalchemy.orm import Session

import speech_recognition as sr
from pydub import AudioSegment

from database import engine, Base, get_db
import models

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

# --- АВТО-МИГРАЦИЯ СТРУКТУРЫ БД ---
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS currency VARCHAR DEFAULT 'AMD';"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR DEFAULT 'ru';"))
        conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'expense';"))
        conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS currency VARCHAR DEFAULT 'AMD';"))
        conn.commit()
        print("Database schema successfully migrated!")
except Exception as e:
    print(f"Migration notice: {e}")

Base.metadata.create_all(bind=engine)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aura-planner-ejyi.onrender.com")

app = FastAPI(title="Aura OS Gold API")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
recognizer = sr.Recognizer()

# --- КУРСЫ ВАЛЮТ (AMD / USD / RUB) ---
RATES_TO_USD = {
    "USD": 1.0,
    "AMD": 0.00258,  # ~388 AMD za 1 USD
    "RUB": 0.011     # ~90 RUB za 1 USD
}

def convert_currency(amount: float, from_curr: str, to_curr: str) -> float:
    if from_curr == to_curr or amount == 0:
        return amount
    amount_in_usd = amount * RATES_TO_USD.get(from_curr, 1.0)
    target_rate = RATES_TO_USD.get(to_curr, 1.0)
    return round(amount_in_usd / target_rate, 2)

def recognize_speech_free(audio_bytes: bytes) -> str:
    try:
        audio_stream = io.BytesIO(audio_bytes)
        sound = AudioSegment.from_file(audio_stream)
        wav_stream = io.BytesIO()
        sound.export(wav_stream, format="wav")
        wav_stream.seek(0)

        with sr.AudioFile(wav_stream) as source:
            audio_data = recognizer.record(source)
            try:
                text_hy = recognizer.recognize_google(audio_data, language="hy-AM")
                if text_hy and len(text_hy.strip()) > 0:
                    return text_hy
            except sr.UnknownValueError:
                pass
            
            try:
                text_ru = recognizer.recognize_google(audio_data, language="ru-RU")
                if text_ru and len(text_ru.strip()) > 0:
                    return text_ru
            except sr.UnknownValueError:
                pass
    except Exception as e:
        print(f"Audio processing error: {e}")
    return ""

class UserSettings(BaseModel):
    telegram_id: int
    currency: Optional[str] = None
    language: Optional[str] = None

class RecordCreate(BaseModel):
    telegram_id: int
    title: str

class ShortcutPayload(BaseModel):
    text: str

# --- ПАРСЕР ЧИСЕЛ И ВАЛЮТ ---
def parse_and_save(telegram_id: int, text: str, db: Session):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id, currency="AMD", language="ru")
        db.add(user)
        db.commit()

    base_currency = user.currency or "AMD"
    category = "task"
    rec_type = "expense"
    amount = 0.0
    detected_currency = None
    text_lower = text.lower()

    if any(k in text_lower for k in ["доллар", "dollar", "dolar", "$", "դոլար"]):
        detected_currency = "USD"
    elif any(k in text_lower for k in ["рубл", "руб", "rub", "ռուբլի"]):
        detected_currency = "RUB"
    elif any(k in text_lower for k in ["драм", "dram", "֏", "դրամ"]):
        detected_currency = "AMD"
    else:
        detected_currency = base_currency

    normalized_text = re.sub(r'(\d+)[\.,\s](\d{3})\b', r'\1\2', text_lower)
    numbers = re.findall(r'\d+(?:\.\d+)?', normalized_text)
    
    if numbers:
        amount = float(numbers[0])
        if any(k in text_lower for k in ["млн", "миллион", "միլիոն", "million"]):
            if amount < 1000000:
                amount *= 1000000
        elif any(k in text_lower for k in ["тыс", "հազար", "k", "thousand"]):
            if amount < 1000:
                amount *= 1000
    else:
        units = {
            "մեկ": 1, "մեկը": 1, "երկու": 2, "երեք": 3, "չորս": 4, "հինգ": 5,
            "վեց": 6, "յոթ": 7, "ութ": 8, "ինը": 9, "ինն": 9,
            "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
            "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "one": 1, "two": 2
        }
        tens = {
            "տաս": 10, "տասն": 10, "քսան": 20, "երեսուն": 30, "քառասուն": 40, "հիսուն": 50,
            "վաթսուն": 60, "յոթանասուն": 70, "ութսուն": 80, "իննսուն": 90,
            "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
            "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90
        }
        hundreds = {
            "հարյուր": 100, "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
            "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900
        }

        words = re.findall(r'\w+', text_lower)
        total = 0.0
        curr_val = 0.0

        for w in words:
            if w in units:
                curr_val += units[w]
            elif w in tens:
                curr_val += tens[w]
            elif w in hundreds:
                curr_val += hundreds[w]
            elif w in ["հազար", "тысяча", "тысячи", "тысяч", "тыс"]:
                if curr_val == 0:
                    curr_val = 1
                total += curr_val * 1000
                curr_val = 0
            elif w in ["միլիոն", "միլիոնն", "մլն", "миллион", "миллиона", "миллионов", "млн", "million"]:
                if curr_val == 0:
                    curr_val = 1
                total += curr_val * 1000000
                curr_val = 0

        total += curr_val
        amount = float(total)

    income_triggers = [
        "зарплат", "получк", "аванс", "преми", "калым", "доход", "получил", "перевод", "прибыль", 
        "пополнен", "продаж", "дивиденд", "кэшбэк", "кешбек", "стейкинг", "крипт", "процент", "подарок", "фриланс",
        "ստացա", "եկամուտ", "աշխատավարձ", "փոխանցում", "նվեր", "վաճառք", "շահույթ", "կանխավճար", "մուտք", "ավելացավ", "եկամուտներ",
        "salary", "paycheck", "income", "bonus", "profit", "gift", "crypto", "cashback", "dividend", "sale", "freelance"
    ]
    expense_triggers = [
        "руб", "$", "драм", "֏", "купил", "потратил", "цена", "стоил", "кофе", "заправк", "бензин", "ремонт", "оплат",
        "еда", "ужин", "обед", "завтрак", "ресторан", "кафе", "продукты", "такси", "парикмахер", "аренда",
        "коммунал", "связь", "интернет", "аптек", "врач", "bmw", "запчаст", "масло", "сервис", "мойк",
        "ծախս", "գնեցի", "սուրճ", "կոֆե", "ինվեստ", "բենզին", "ավտո", "տաքսի", "վարձ", "ուտելիք", "հաց", "դեղ", "սպասարկում",
        "bought", "paid", "spent", "coffee", "food", "taxi", "rent", "bmw", "parts", "auto", "gas", "petrol", "dinner"
    ]

    if any(k in text_lower for k in income_triggers):
        category = "finance"
        rec_type = "income"
    elif amount > 0 or any(k in text_lower for k in expense_triggers):
        category = "finance"
        rec_type = "expense"

    if category == "finance" and amount > 0:
        amount = convert_currency(amount, detected_currency, base_currency)

    record = models.Record(
        user_id=user.id,
        category=category,
        type=rec_type,
        title=text,
        amount=round(amount, 2),
        currency=base_currency
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
    return HTMLResponse(content="<h1>Index file not found</h1>", status_code=404)

@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/api/user/{telegram_id}")
def get_user_info(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id, currency="AMD", language="ru")
        db.add(user)
        db.commit()
        db.refresh(user)
    return {"currency": user.currency, "language": user.language or "ru"}

@app.post("/api/user/settings")
def update_user_settings(data: UserSettings, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    if user:
        if data.currency:
            user.currency = data.currency
        if data.language:
            user.language = data.language
        db.commit()
    return {"status": "ok", "currency": user.currency, "language": user.language}

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
        "type": record.type,
        "amount": record.amount,
        "currency": record.currency
    }

@app.delete("/api/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.Record).filter(models.Record.id == record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(record)
    db.commit()
    return {"status": "deleted"}

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
        "type": rec.type,
        "amount": rec.amount,
        "currency": rec.currency
    }

@app.post("/api/shortcut")
def handle_shortcut(id: int, payload: ShortcutPayload, db: Session = Depends(get_db)):
    record = parse_and_save(id, payload.text, db)
    return {
        "status": "ok",
        "category": record.category,
        "type": record.type,
        "amount": record.amount,
        "currency": record.currency,
        "text": record.title
    }

# --- TELEGRAM BOT ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Открыть Aura OS Gold", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        f"🎙 Напиши или надиктуй задачу/доход/расход!",
        reply_markup=markup
    )

@dp.message(F.text)
async def handle_text_message(message: types.Message):
    db = next(get_db())
    rec = parse_and_save(message.from_user.id, message.text, db)
    emoji = "📈" if rec.type == "income" else ("💸" if rec.category == "finance" else "✅")
    await message.answer(f"{emoji} Записано: **{rec.title}** ({rec.amount:.2f} {rec.currency})", parse_mode="Markdown")

@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    db = next(get_db())
    file_info = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    
    recognized_text = recognize_speech_free(file_bytes.read())
    if not recognized_text:
        recognized_text = "Голосовая запись"

    rec = parse_and_save(message.from_user.id, recognized_text, db)
    emoji = "📈" if rec.type == "income" else ("💸" if rec.category == "finance" else "✅")
    await message.answer(f"🎙 {emoji} **Распознано:** «{rec.title}»\nСумма: **{rec.amount:.2f} {rec.currency}**", parse_mode="Markdown")

async def keep_alive():
    await asyncio.sleep(30)
    ping_url = f"{WEBAPP_URL}/ping"
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get(ping_url)
            except Exception:
                pass
            await asyncio.sleep(600)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(keep_alive())