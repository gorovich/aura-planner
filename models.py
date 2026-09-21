from sqlalchemy import Column, Integer, BigInteger, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    language = Column(String, default="ru")
    currency = Column(String, default="AMD")
    
    # Статусы администрирования и доступов
    is_admin = Column(Boolean, default=False)
    is_blocked = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)
    bot_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)

    records = relationship("Record", back_populates="user")

class Record(Base):
    __tablename__ = "records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    category = Column(String, default="task")          # 'task' или 'finance'
    sub_category = Column(String, default="general")   # 'auto', 'food', 'rent', 'sub', 'general'
    type = Column(String, default="expense")           # 'expense' или 'income'
    title = Column(String)
    amount = Column(Float, default=0.0)
    currency = Column(String, default="AMD")
    created_at = Column(DateTime, default=datetime.utcnow)

    # --- ПОЛЯ ДЛЯ ПЛАНЕРА И НАПОМИНАНИЙ ---
    status = Column(String, default="pending")         # 'pending' или 'completed'
    due_date = Column(DateTime, nullable=True)         # Дата и время дедлайна / напоминания
    is_recurring = Column(Boolean, default=False)      # Флаг повторяющегося платежа/задачи
    recurrence_rule = Column(String, nullable=True)    # 'monthly', 'weekly', 'daily'
    is_reminded = Column(Boolean, default=False)       # Было ли отправлено уведомление

    user = relationship("User", back_populates="records")