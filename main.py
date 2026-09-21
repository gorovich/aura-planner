import os
import re
import io
import random
import string
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone, timedelta, date
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

from database import engine, Base, get_db, SessionLocal
import models

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import (
    WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, 
    LabeledPrice, PreCheckoutQuery, Message
)

# --- КОНФИГУРАЦИЯ ---
ADMIN_TELEGRAM_ID = 1689610141

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aura-planner-ejyi.onrender.com")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
recognizer = sr.Recognizer()


# --- МИГРАЦИИ БД ---
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
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_until TIMESTAMP;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS streak_count INTEGER DEFAULT 1;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_streak_date DATE;"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS family_id INTEGER;"))
            
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'expense';"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS currency VARCHAR DEFAULT 'AMD';"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS sub_category VARCHAR DEFAULT 'general';"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'pending';"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS due_date TIMESTAMP;"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS recurrence_rule VARCHAR;"))
            conn.execute(text("ALTER TABLE records ADD COLUMN IF NOT EXISTS is_reminded BOOLEAN DEFAULT FALSE;"))
            
            conn.execute(text(f"UPDATE users SET is_admin = TRUE WHERE telegram_id = {ADMIN_TELEGRAM_ID};"))
            conn.commit()
            print("Database schema successfully migrated!")
    except Exception as e:
        print(f"Migration notice: {e}")

    Base.metadata.create_all(bind=engine)


# --- ПРОВЕРКИ СТАТУСОВ ПОДПИСКИ ---
def get_user_sub_info(user: models.User):
    now = datetime.utcnow()
    if user.is_admin:
        return {"type": "admin", "days_left": 999, "is_premium": True}
    
    if user.is_premium or (user.premium_until and user.premium_until > now):
        days = 0
        if user.premium_until and user.premium_until > now:
            days = (user.premium_until - now).days + 1
        return {"type": "paid", "days_left": days, "is_premium": True}
    
    if user.trial_until and user.trial_until > now:
        days = (user.trial_until - now).days + 1
        return {"type": "trial", "days_left": days, "is_premium": True}
    
    return {"type": "free", "days_left": 0, "is_premium": False}

def update_streak(user: models.User, db: Session):
    today = date.today()
    if user.last_streak_date == today:
        return
    if user.last_streak_date == today - timedelta(days=1):
        user.streak_count += 1
    else:
        user.streak_count = 1
    user.last_streak_date = today
    db.commit()


# --- ПЛАНИРОВЩИК ЗАДАЧ ---
scheduler = AsyncIOScheduler()

async def check_reminders_and_deadlines():
    with SessionLocal() as db:
        try:
            yerevan_tz = timezone(timedelta(hours=4))
            now = datetime.now(yerevan_tz).replace(tzinfo=None)

            pending_records = db.query(models.Record).filter(
                models.Record.due_date <= now,
                models.Record.is_reminded == False,
                models.Record.status == "pending"
            ).all()

            for rec in pending_records:
                user = db.query(models.User).filter(models.User.id == rec.user_id, models.User.bot_active == True).first()
                if user:
                    icon = "⏰" if rec.category == "task" else "💳"
                    msg = f"{icon} **Напоминание / Дедлайн!**\n\n**{rec.title}**"
                    if rec.amount > 0:
                        msg += f"\nСумма: `{rec.amount:.2f} {rec.currency}`"
                    try:
                        await bot.send_message(user.telegram_id, msg, parse_mode="Markdown")
                        rec.is_reminded = True
                        if rec.is_recurring and rec.recurrence_rule == "monthly":
                            rec.due_date = rec.due_date + timedelta(days=30)
                            rec.is_reminded = False
                        db.commit()
                    except Exception as e:
                        print(f"Reminder send error: {e}")
        except Exception as e:
            print(f"Reminder task error: {e}")

async def send_daily_digest():
    with SessionLocal() as db:
        try:
            yerevan_tz = timezone(timedelta(hours=4))
            now = datetime.now(yerevan_tz)
            users = db.query(models.User).filter(models.User.bot_active == True, models.User.is_blocked == False).all()
            for user in users:
                records = db.query(models.Record).filter(models.Record.user_id == user.id).all()
                inc_total = sum(r.amount for r in records if r.type == "income")
                exp_total = sum(r.amount for r in records if r.type == "expense")
                tasks_cnt = sum(1 for r in records if r.category == "task" and r.status == "pending")
                curr = user.currency or "AMD"
                msg = (
                    f"🌙 **Вечерний Дайджест Aura OS** ({now.strftime('%d.%m')})\n\n"
                    f"📈 Доходы: `{inc_total:.2f} {curr}`\n"
                    f"💸 Расходы: `{exp_total:.2f} {curr}`\n"
                    f"💰 Свободный Баланс: `{(inc_total - exp_total):.2f} {curr}`\n"
                    f"✅ Активных задач: `{tasks_cnt}`\n\n"
                    f"Хорошего вечера!"
                )
                try:
                    await bot.send_message(user.telegram_id, msg, parse_mode="Markdown")
                except Exception:
                    user.bot_active = False
                    db.commit()
        except Exception as e:
            print(f"Digest error: {e}")

async def run_bot():
    await asyncio.sleep(4)
    try: await bot.delete_webhook(drop_pending_updates=True)
    except Exception: pass
    await dp.start_polling(bot, handle_signals=False)

async def keep_alive():
    await asyncio.sleep(30)
    ping_url = f"{WEBAPP_URL}/ping"
    async with httpx.AsyncClient() as client:
        while True:
            try: await client.get(ping_url)
            except Exception: pass
            await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(asyncio.to_thread(run_db_migrations))
    scheduler.add_job(check_reminders_and_deadlines, 'interval', minutes=1)
    scheduler.add_job(send_daily_digest, CronTrigger(hour=21, minute=0, timezone="Asia/Yerevan"))
    scheduler.start()
    bot_task = asyncio.create_task(run_bot())
    keep_alive_task = asyncio.create_task(keep_alive())
    yield
    scheduler.shutdown()
    bot_task.cancel()
    keep_alive_task.cancel()


app = FastAPI(title="Aura OS Royal Gold API", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.exists(STATIC_DIR): os.makedirs(STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
@app.get("/ping")
async def health_check():
    return {"status": "ok", "online": True}


# --- ВАЛЮТЫ И АУДИО ---
RATES_TO_USD = { "USD": 1.0, "AMD": 0.00258, "RUB": 0.011 }

def convert_currency(amount: float, from_curr: str, to_curr: str) -> float:
    if from_curr == to_curr or amount == 0: return amount
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
                if text_hy and len(text_hy.strip()) > 0: return text_hy
            except sr.UnknownValueError: pass
            
            try:
                text_ru = recognizer.recognize_google(audio_data, language="ru-RU")
                if text_ru and len(text_ru.strip()) > 0: return text_ru
            except sr.UnknownValueError: pass
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
    category: Optional[str] = None
    sub_category: Optional[str] = None
    type: Optional[str] = None
    due_date: Optional[str] = None
    is_recurring: Optional[bool] = False

class GoalCreate(BaseModel):
    telegram_id: int
    title: str
    target_amount: float
    icon: Optional[str] = "🎯"

class GoalDeposit(BaseModel):
    telegram_id: int
    amount: float

class FamilyJoin(BaseModel):
    telegram_id: int
    code: str

class StarsInvoiceRequest(BaseModel):
    telegram_id: int
    months: int

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
def parse_and_save(
    telegram_id: int, 
    text: str, 
    db: Session, 
    first_name: str = None, 
    username: str = None,
    category_override: Optional[str] = None,
    sub_category_override: Optional[str] = None,
    type_override: Optional[str] = None,
    due_date_override: Optional[datetime] = None,
    is_recurring: bool = False
):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    is_adm = (telegram_id == ADMIN_TELEGRAM_ID)
    
    if not user:
        user = models.User(
            telegram_id=telegram_id, currency="AMD", language="ru",
            first_name=first_name, username=username, is_admin=is_adm
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

    sub_info = get_user_sub_info(user)
    if not sub_info["is_premium"]:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = db.query(models.Record).filter(
            models.Record.user_id == user.id,
            models.Record.created_at >= today_start
        ).count()
        if today_count >= 3:
            raise HTTPException(
                status_code=402, 
                detail="Достигнут лимит на сегодня (3/3). Перейдите на Royal Gold для безлимита!"
            )

    update_streak(user, db)

    base_currency = user.currency or "AMD"
    category = category_override or "task"
    sub_category = sub_category_override or "general"
    rec_type = type_override or "expense"
    amount = 0.0
    text_lower = text.lower()

    normalized_text = re.sub(r'(\d+)[\.,\s](\d{3})\b', r'\1\2', text_lower)
    numbers = re.findall(r'\d+(?:\.\d+)?', normalized_text)
    
    if numbers:
        amount = float(numbers[0])
        if any(k in text_lower for k in ["млн", "миллион", "միլիոն"]): amount *= 1000000
        elif any(k in text_lower for k in ["тыс", "հազար", "k"]): amount *= 1000

    if not category_override and not type_override:
        income_triggers = ["зарплат", "получк", "аванс", "преми", "доход", "получил", "перевод", "ստացա", "եկամուտ", "salary"]
        expense_triggers = ["руб", "$", "драм", "֏", "купил", "потратил", "кофе", "заправк", "бензин", "ремонт", "еда", "такси", "ծախս", "սուրճ"]

        if any(k in text_lower for k in income_triggers):
            category = "finance"
            rec_type = "income"
        elif amount > 0 or any(k in text_lower for k in expense_triggers):
            category = "finance"
            rec_type = "expense"

    record = models.Record(
        user_id=user.id,
        category=category,
        sub_category=sub_category,
        type=rec_type,
        title=text,
        amount=round(amount, 2),
        currency=base_currency,
        due_date=due_date_override,
        is_recurring=is_recurring
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
        with open(index_file, "r", encoding="utf-8") as f: return f.read()
    return HTMLResponse(content="<h1>Index file not found</h1>", status_code=404)

@app.get("/api/user/{telegram_id}")
def get_user_info(telegram_id: int, first_name: Optional[str] = None, username: Optional[str] = None, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    is_adm = (telegram_id == ADMIN_TELEGRAM_ID)
    
    if not user:
        user = models.User(telegram_id=telegram_id, currency="AMD", language="ru", is_admin=is_adm, first_name=first_name, username=username)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.last_active_at = datetime.utcnow()
        if first_name and first_name != "undefined": user.first_name = first_name
        if username and username != "undefined": user.username = username
        if is_adm and not user.is_admin: user.is_admin = True
        db.commit()

    sub_info = get_user_sub_info(user)
    family_code = user.family.code if user.family else None

    return {
        "currency": user.currency,
        "language": user.language or "ru",
        "is_admin": user.is_admin,
        "is_premium": sub_info["is_premium"],
        "sub_type": sub_info["type"],
        "days_left": sub_info["days_left"],
        "is_blocked": user.is_blocked,
        "streak_count": user.streak_count or 1,
        "family_code": family_code
    }

@app.post("/api/user/settings")
def update_user_settings(data: UserSettings, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    if user:
        if data.currency: user.currency = data.currency
        if data.language: user.language = data.language
        db.commit()
    return {"status": "ok"}

@app.get("/api/records/{telegram_id}")
def get_records(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user: return []
    
    if user.family_id:
        family_user_ids = [u.id for u in db.query(models.User).filter(models.User.family_id == user.family_id).all()]
        return db.query(models.Record).filter(models.Record.user_id.in_(family_user_ids)).order_by(models.Record.id.desc()).all()

    return db.query(models.Record).filter(models.Record.user_id == user.id).order_by(models.Record.id.desc()).all()

@app.post("/api/records")
def create_record(data: RecordCreate, db: Session = Depends(get_db)):
    parsed_date = None
    if data.due_date:
        try: parsed_date = datetime.fromisoformat(data.due_date)
        except Exception: pass

    record = parse_and_save(
        data.telegram_id, data.title, db,
        category_override=data.category,
        sub_category_override=data.sub_category,
        type_override=data.type,
        due_date_override=parsed_date,
        is_recurring=data.is_recurring or False
    )
    return {
        "status": "ok", "id": record.id, "title": record.title,
        "category": record.category, "sub_category": record.sub_category,
        "type": record.type, "amount": record.amount, "currency": record.currency
    }

@app.patch("/api/records/{record_id}/status")
def toggle_record_status(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.Record).filter(models.Record.id == record_id).first()
    if not record: raise HTTPException(status_code=404, detail="Not found")
    record.status = "completed" if record.status == "pending" else "pending"
    db.commit()
    return {"status": "ok", "new_status": record.status}

@app.delete("/api/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    record = db.query(models.Record).filter(models.Record.id == record_id).first()
    if not record: raise HTTPException(status_code=404, detail="Not found")
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
    recognized_text = recognize_speech_free(audio_bytes) or "Голосовая запись"
    rec = parse_and_save(telegram_id, recognized_text, db)
    return { "status": "ok", "id": rec.id, "title": rec.title, "category": rec.category, "type": rec.type, "amount": rec.amount, "currency": rec.currency }


# --- API КОПИЛОК И СЕМЬИ ---

@app.get("/api/goals/{telegram_id}")
def get_goals(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user: return []
    if user.family_id:
        return db.query(models.Goal).filter(models.Goal.family_id == user.family_id).all()
    return db.query(models.Goal).filter(models.Goal.user_id == user.id).all()

@app.post("/api/goals")
def create_goal(data: GoalCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found")
    goal = models.Goal(
        user_id=user.id,
        family_id=user.family_id,
        title=data.title,
        target_amount=data.target_amount,
        currency=user.currency or "AMD",
        icon=data.icon or "🎯"
    )
    db.add(goal)
    db.commit()
    return {"status": "ok"}

@app.post("/api/goals/{goal_id}/deposit")
def deposit_goal(goal_id: int, data: GoalDeposit, db: Session = Depends(get_db)):
    goal = db.query(models.Goal).filter(models.Goal.id == goal_id).first()
    if not goal: raise HTTPException(status_code=404, detail="Goal not found")
    goal.current_amount += data.amount
    db.commit()
    return {"status": "ok", "current_amount": goal.current_amount}

@app.post("/api/family/create/{telegram_id}")
def create_family(telegram_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == telegram_id).first()
    if not user: 
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    if user.family:
        return {"status": "ok", "code": user.family.code, "is_existing": True}

    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        existing_family = db.query(models.Family).filter(models.Family.code == code).first()
        if not existing_family:
            break

    family = models.Family(code=code, name=f"Семья {user.first_name or user.telegram_id}")
    db.add(family)
    db.commit()
    db.refresh(family)

    user.family_id = family.id
    db.commit()
    return {"status": "ok", "code": code, "is_existing": False}

@app.post("/api/family/join")
def join_family(data: FamilyJoin, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.telegram_id == data.telegram_id).first()
    family = db.query(models.Family).filter(models.Family.code == data.code.strip().upper()).first()
    if not user or not family: 
        raise HTTPException(status_code=404, detail="Код семьи не найден")
    
    user.family_id = family.id
    db.commit()
    return {"status": "ok", "family_name": family.name}


# --- ОПЛАТА TELEGRAM STARS ---

@app.post("/api/pay/stars")
async def create_stars_invoice(data: StarsInvoiceRequest, db: Session = Depends(get_db)):
    stars_price = 150
    if data.months == 3: stars_price = 350
    elif data.months == 12: stars_price = 990

    prices = [LabeledPrice(label=f"Royal Gold ({data.months} мес.)", amount=stars_price)]
    invoice_link = await bot.create_invoice_link(
        title=f"Aura OS Royal Gold ({data.months} мес.)",
        description=f"Безлимитный доступ ко всем функциям Aura OS на {data.months} мес.",
        payload=f"sub_{data.telegram_id}_{data.months}",
        provider_token="",
        currency="XTR",
        prices=prices
    )
    return {"invoice_url": invoice_link}

@dp.pre_checkout_query()
async def process_pre_checkout_query(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split("_")
    tg_id = int(parts[1])
    months = int(parts[2])

    with SessionLocal() as db:
        user = db.query(models.User).filter(models.User.telegram_id == tg_id).first()
        if user:
            user.is_premium = True
            now = datetime.utcnow()
            base_date = user.premium_until if (user.premium_until and user.premium_until > now) else now
            user.premium_until = base_date + timedelta(days=30 * months)
            db.commit()

    await message.answer(f"🎉 **Оплата зачислена!** Подписка Royal Gold продлена на {months} мес. Спасибо!")


# --- АДМИНСКИЕ ЭНДПОИНТЫ С ПОДРОБНОЙ СТАТИСТИКОЙ ---

@app.get("/api/admin/stats/{admin_id}")
def get_admin_stats(admin_id: int, db: Session = Depends(get_db)):
    if admin_id != ADMIN_TELEGRAM_ID: raise HTTPException(status_code=403, detail="Forbidden")
    return {
        "total_users": db.query(models.User).count(),
        "active_bot_users": db.query(models.User).filter(models.User.bot_active == True).count(),
        "premium_users": db.query(models.User).filter(models.User.is_premium == True).count(),
        "banned_users": db.query(models.User).filter(models.User.is_blocked == True).count(),
        "total_records": db.query(models.Record).count()
    }

@app.get("/api/admin/users/{admin_id}")
def get_admin_users(admin_id: int, db: Session = Depends(get_db)):
    if admin_id != ADMIN_TELEGRAM_ID: raise HTTPException(status_code=403, detail="Forbidden")
    users = db.query(models.User).order_by(models.User.id.desc()).all()
    res = []
    for u in users:
        rec_count = db.query(models.Record).filter(models.Record.user_id == u.id).count()
        sub_info = get_user_sub_info(u)
        res.append({
            "id": u.id,
            "telegram_id": u.telegram_id,
            "first_name": u.first_name or "Без имени",
            "username": u.username or "",
            "sub_type": sub_info["type"],
            "days_left": sub_info["days_left"],
            "is_blocked": u.is_blocked,
            "records_count": rec_count
        })
    return res

@app.post("/api/admin/toggle-premium")
def admin_toggle_premium(data: AdminUserAction, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID: raise HTTPException(status_code=403, detail="Forbidden")
    user = db.query(models.User).filter(models.User.telegram_id == data.target_tg_id).first()
    if user:
        user.is_premium = not user.is_premium
        db.commit()
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/toggle-ban")
def admin_toggle_ban(data: AdminUserAction, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID: raise HTTPException(status_code=403, detail="Forbidden")
    user = db.query(models.User).filter(models.User.telegram_id == data.target_tg_id).first()
    if user:
        user.is_blocked = not user.is_blocked
        db.commit()
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="User not found")

@app.post("/api/admin/broadcast")
async def admin_broadcast(data: AdminBroadcast, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID: raise HTTPException(status_code=403, detail="Forbidden")
    users = db.query(models.User).filter(models.User.bot_active == True, models.User.is_blocked == False).all()
    success_count = 0
    for u in users:
        try:
            await bot.send_message(u.telegram_id, data.message_text, parse_mode="Markdown")
            success_count += 1
        except Exception:
            u.bot_active = False
            db.commit()
    return {"status": "ok", "success": success_count}

@app.post("/api/admin/direct-message")
async def admin_direct_message(data: AdminDirectMessage, db: Session = Depends(get_db)):
    if data.admin_id != ADMIN_TELEGRAM_ID: raise HTTPException(status_code=403, detail="Forbidden")
    target_clean = data.target.strip()
    user = None
    if target_clean.isdigit(): user = db.query(models.User).filter(models.User.telegram_id == int(target_clean)).first()
    else: user = db.query(models.User).filter(models.User.username.ilike(f"%{target_clean.lstrip('@')}%")).first()
    if not user: raise HTTPException(status_code=404, detail="Пользователь не найден")
    await bot.send_message(user.telegram_id, data.message_text, parse_mode="Markdown")
    return {"status": "ok", "recipient": user.first_name or str(user.telegram_id)}


# --- TELEGRAM BOT HANDLERS ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    db = next(get_db())
    parse_and_save(message.from_user.id, "", db, first_name=message.from_user.first_name, username=message.from_user.username)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👑 Открыть Aura OS Gold", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await message.answer(f"Привет, {message.from_user.first_name}! 👋\n\n👑 Добро пожаловать в Aura OS Royal Gold!", reply_markup=markup)

@dp.message(F.text)
async def handle_text_message(message: types.Message):
    db = next(get_db())
    rec = parse_and_save(message.from_user.id, message.text, db, first_name=message.from_user.first_name, username=message.from_user.username)
    emoji = "📈" if rec.type == "income" else ("💸" if rec.category == "finance" else "✅")
    await message.answer(f"{emoji} Записано: **{rec.title}**\nСумма: **{rec.amount:.2f} {rec.currency}**", parse_mode="Markdown")