import os
import re
import io
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager
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
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, KICKED, MEMBER
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated

# --- КОНФИГУРАЦИЯ ---
ADMIN_TELEGRAM_ID = 1689610141

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aura-planner-ejyi.onrender.com")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
recognizer = sr.Recognizer()


# --- СИНХРОННЫЕ МИГРАЦИИ БД (ВЫНЕСЕНЫ ИЗ ГЛАВНОГО ПОТОКА) ---
def run_db_migrations():
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS currency VARCHAR DEFAULT 'AMD';"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR DEFAULT 'ru';"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_active BOOLEAN DEFAULT TRUE;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"))
            
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'expense';"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS currency VARCHAR DEFAULT 'AMD';"))
            
            conn.execute(text(f"UPDATE users SET is_admin = TRUE WHERE telegram_id = {ADMIN_TELEGRAM_ID};"))
            conn.commit()
            print("Database schema successfully migrated!")
    except Exception as e:
        print(f"Migration notice: {e}")

    Base.metadata.create_all(bind=engine)


# --- ФОНОВЫЕ ЗАДАЧИ ---
async def run_bot():
    await asyncio.sleep(3)
    await dp.start_polling(bot, handle_signals=False)

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

async def daily_digest_scheduler():
    last_sent_date = None
    while True:
        try:
            yerevan_tz = timezone(timedelta(hours=4))
            now = datetime.now(yerevan_tz)
            
            if now.hour == 21 and last_sent_date != now.date():
                db = next(get_db())
                users = db.query(models.User).filter(models.User.bot_active == True, models.User.is_blocked == False).all()
                
                for user in users:
                    records = db.query(models.Record).filter(models.Record.user_id == user.id).all()
                    inc_total = sum(r.amount for r in records if r.type == "income")
                    exp_total = sum(r.amount for r in records if r.type == "expense")
                    tasks_cnt = sum(1 for r in records if r.category == "task")
                    
                    curr = user.currency or "AMD"
                    msg = (
                        f"🌙 **Вечерний Дайджест Aura OS** ({now.strftime('%d.%m')})\n\n"
                        f"📈 Доходы за всё время: `{inc_total:.2f} {curr}`\n"
                        f"💸 Расходы за всё время: `{exp_total:.2f} {curr}`\n"
                        f"💰 Свободный Баланс: `{(inc_total - exp_total):.2f} {curr}`\n"
                        f"✅ Активных задач: `{tasks_cnt}`\n\n"
                        f"Хорошего вечера!"
                    )
                    try:
                        await bot.send_message(user.telegram_id, msg, parse_mode="Markdown")
                    except Exception:
                        user.bot_active = False
                        db.commit()
                
                last_sent_date = now.date()
        except Exception as e:
            print(f"Digest error: {e}")
            
        await asyncio.sleep(300)


# --- ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ (LIFESPAN) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Выполняем синхронную работу с БД в отдельном потоке (to_thread)
    # Это предотвращает заморозку Event Loop при запуске Uvicorn
    await asyncio.to_thread(run_db_migrations)

    bot_task = asyncio.create_task(run_bot())
    keep_alive_task = asyncio.create_task(keep_alive())
    digest_task = asyncio.create_task(daily_digest_scheduler())

    yield

    bot_task.cancel()
    keep_alive_task.cancel()
    digest_task.cancel()


# --- ИНИЦИАЛИЗАЦИЯ FASTAPI И СТАТИКИ ---
app = FastAPI(title="Aura OS Royal Gold API", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- HEALTH CHECK ---
@app.get("/healthz")
@app.get("/ping")
async def health_check():
    return {"status": "ok", "online": True}


# --- КУРСЫ ВАЛЮТ И ОБРАБОТКА АУДИО ---
RATES_TO_USD = {
    "USD": 1.0,
    "AMD": 0.00258,
    "RUB": 0.011
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


# --- PYDANTIC МОДЕЛИ ---
class UserSettings(BaseModel):
    telegram_id: int
    currency: Optional[str] = None
    language: Optional[str] = None

class RecordCreate(BaseModel):
    telegram_id: int
    title: str

class ShortcutPayload(BaseModel):
    text: str

class AdminUserAction(BaseModel):
    admin_id: int
    target_tg_id: int

class AdminBroadcast(BaseModel):
    admin_id: int
    message_text: str

class AdminDirectMessage(BaseModel):
    admin_id: int
    target: str
    message_text: str


# --- ПАРСЕР И РЕГИСТРАЦИЯ ЮЗЕРА ---
def parse_and_save(telegram_id: int, text: str, db: Session, first_name: str = None, username: str = None):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    is_adm = (telegram_id == ADMIN_TELEGRAM_ID)
    
    if not user:
        user = models.User(
            telegram_id=telegram_id, 
            currency="AMD", 
            language="ru",
            first_name=first_name,
            username=username,
            is_admin=is_adm
        )
        db.add(user)
        db.commit()
    else:
        user.last_active_at = datetime.utcnow()
        if first_name and first_name != "undefined": user.first_name = first_name
        if username and username != "undefined": user.username = username
        if is_adm: user.is_admin = True
        db.commit()

    if user.is_blocked:
        raise HTTPException(status_code=403, detail="Пользователь заблокирован")

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

@app.get("/api/user/{telegram_id}")
def get_user_info(
    telegram_id: int, 
    first_name: Optional[str] = None, 
    username: Optional[str] = None, 
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    is_adm = (telegram_id == ADMIN_TELEGRAM_ID)
    
    if not user:
        user = models.User(
            telegram_id=telegram_id, 
            currency="AMD", 
            language="ru", 
            is_admin=is_adm,
            first_name=first_name,
            username=username
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.last_active_at = datetime.utcnow()
        if first_name and first_name != "undefined": user.first_name = first_name
        if username and username != "undefined": user.username = username
        if is_adm and not user.is_admin: user.is_admin = True
        db.commit()

    return {
        "currency": user.currency, 
        "language": user.language or "ru",
        "is_admin": user.is_admin,
        "is_premium": user.is_premium,
        "is_blocked": user.is_blocked
    }

@app.post("/api/user/settings")
def update_user_settings(data: UserSettings, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    if user:
        if data.currency: user.currency = data.currency
        if data.language: user.language = data.language
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


# --- ADMIN API ENDPOINTS ---

@app.get("/api/admin/stats/{admin_id}")
def get_admin_stats(admin_id: int, db: Session = Depends(get_db)):
    admin = db.query(models.User).filter(models.User.telegram_id == admin_id, models.User.is_admin == True).first()
    if not admin and admin_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    total_users = db.query(models.User).count()
    active_bot_users = db.query(models.User).filter(models.User.bot_active == True).count()
    blocked_bot_users = db.query(models.User).filter(models.User.bot_active == False).count()
    premium_users = db.query(models.User).filter(models.User.is_premium == True).count()
    banned_users = db.query(models.User).filter(models.User.is_blocked == True).count()
    total_records = db.query(models.Record).count()

    return {
        "total_users": total_users,
        "active_bot_users": active_bot_users,
        "blocked_bot_users": blocked_bot_users,
        "premium_users": premium_users,
        "banned_users": banned_users,
        "total_records": total_records
    }

@app.get("/api/admin/users/{admin_id}")
def get_admin_users(admin_id: int, db: Session = Depends(get_db)):
    admin = db.query(models.User).filter(models.User.telegram_id == admin_id, models.User.is_admin == True).first()
    if not admin and admin_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    users = db.query(models.User).order_by(models.User.id.desc()).all()
    res = []
    for u in users:
        rec_count = db.query(models.Record).filter(models.Record.user_id == u.id).count()
        res.append({
            "id": u.id,
            "telegram_id": u.telegram_id,
            "first_name": u.first_name or "Без имени",
            "username": u.username or "",
            "language": u.language,
            "currency": u.currency,
            "is_premium": u.is_premium,
            "is_blocked": u.is_blocked,
            "bot_active": u.bot_active,
            "records_count": rec_count,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else ""
        })
    return res

@app.post("/api/admin/toggle-premium")
def admin_toggle_premium(data: AdminUserAction, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    user = db.query(models.User).filter(models.User.telegram_id == data.target_tg_id).first()
    if user:
        user.is_premium = not user.is_premium
        db.commit()
        return {"status": "ok", "is_premium": user.is_premium}
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/toggle-ban")
def admin_toggle_ban(data: AdminUserAction, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    user = db.query(models.User).filter(models.User.telegram_id == data.target_tg_id).first()
    if user:
        user.is_blocked = not user.is_blocked
        db.commit()
        return {"status": "ok", "is_blocked": user.is_blocked}
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/broadcast")
async def admin_broadcast(data: AdminBroadcast, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    users = db.query(models.User).filter(models.User.bot_active == True, models.User.is_blocked == False).all()
    success_count = 0
    fail_count = 0

    for u in users:
        try:
            await bot.send_message(u.telegram_id, data.message_text, parse_mode="Markdown")
            success_count += 1
        except Exception:
            fail_count += 1
            u.bot_active = False
            db.commit()

    return {"status": "ok", "success": success_count, "failed": fail_count}

@app.post("/api/admin/direct-message")
async def admin_direct_message(data: AdminDirectMessage, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    target_clean = data.target.strip()
    if not target_clean or not data.message_text.strip():
        raise HTTPException(status_code=400, detail="Укажите адресата и текст сообщения")

    user = None
    if target_clean.isdigit():
        user = db.query(models.User).filter(models.User.telegram_id == int(target_clean)).first()
    else:
        uname = target_clean.lstrip('@')
        user = db.query(models.User).filter(models.User.username.ilike(uname)).first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден в базе")

    try:
        await bot.send_message(user.telegram_id, data.message_text, parse_mode="Markdown")
        return {"status": "ok", "recipient": user.first_name or user.username or str(user.telegram_id)}
    except Exception as e:
        user.bot_active = False
        db.commit()
        raise HTTPException(status_code=400, detail=f"Ошибка отправки: {e}")


# --- TELEGRAM BOT И ХЭНДЛЕРЫ ---

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def user_blocked_bot(event: ChatMemberUpdated):
    db = next(get_db())
    user = db.query(models.User).filter(models.User.telegram_id == event.from_user.id).first()
    if user:
        user.bot_active = False
        db.commit()

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def user_unblocked_bot(event: ChatMemberUpdated):
    db = next(get_db())
    user = db.query(models.User).filter(models.User.telegram_id == event.from_user.id).first()
    if user:
        user.bot_active = True
        db.commit()

def get_record_keyboard(record_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Доход / Расход", callback_data=f"toggle_type:{record_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_rec:{record_id}")
        ]
    ])

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    db = next(get_db())
    parse_and_save(
        message.from_user.id, 
        "", 
        db, 
        first_name=message.from_user.first_name, 
        username=message.from_user.username
    )
    
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
    rec = parse_and_save(
        message.from_user.id, 
        message.text, 
        db, 
        first_name=message.from_user.first_name, 
        username=message.from_user.username
    )
    emoji = "📈" if rec.type == "income" else ("💸" if rec.category == "finance" else "✅")
    
    await message.answer(
        f"{emoji} Записано: **{rec.title}**\n"
        f"Тип: **{rec.type.upper()}** | Сумма: **{rec.amount:.2f} {rec.currency}**",
        parse_mode="Markdown",
        reply_markup=get_record_keyboard(rec.id)
    )

@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    db = next(get_db())
    file_info = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    
    recognized_text = recognize_speech_free(file_bytes.read())
    if not recognized_text:
        recognized_text = "Голосовая запись"

    rec = parse_and_save(
        message.from_user.id, 
        recognized_text, 
        db, 
        first_name=message.from_user.first_name, 
        username=message.from_user.username
    )
    emoji = "📈" if rec.type == "income" else ("💸" if rec.category == "finance" else "✅")
    
    await message.answer(
        f"🎙 {emoji} **Распознано:** «{rec.title}»\n"
        f"Тип: **{rec.type.upper()}** | Сумма: **{rec.amount:.2f} {rec.currency}**",
        parse_mode="Markdown",
        reply_markup=get_record_keyboard(rec.id)
    )

@dp.callback_query(F.data.startswith("toggle_type:"))
async def cb_toggle_type(callback: CallbackQuery):
    rec_id = int(callback.data.split(":")[1])
    db = next(get_db())
    rec = db.query(models.Record).filter(models.Record.id == rec_id).first()
    if rec:
        rec.type = "income" if rec.type == "expense" else "expense"
        db.commit()
        emoji = "📈" if rec.type == "income" else "💸"
        await callback.message.edit_text(
            f"{emoji} Изменено: **{rec.title}**\n"
            f"Новый тип: **{rec.type.upper()}** | Сумма: **{rec.amount:.2f} {rec.currency}**",
            parse_mode="Markdown",
            reply_markup=get_record_keyboard(rec.id)
        )
        await callback.answer("Тип записи изменен!")
    else:
        await callback.answer("Запись не найдена", show_alert=True)

@dp.callback_query(F.data.startswith("del_rec:"))
async def cb_delete_rec(callback: CallbackQuery):
    rec_id = int(callback.data.split(":")[1])
    db = next(get_db())
    rec = db.query(models.Record).filter(models.Record.id == rec_id).first()
    if rec:
        db.delete(rec)
        db.commit()
        await callback.message.edit_text("🗑 Запись удалена!", parse_mode="Markdown")
        await callback.answer("Удалено")
    else:
        await callback.answer("Запись не найдена", show_alert=True)