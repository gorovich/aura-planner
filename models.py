from sqlalchemy import Column, Integer, BigInteger, String, Float, ForeignKey, DateTime, Boolean, Date
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
from database import Base

class Family(Base):
    __tablename__ = "families"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)
    name = Column(String, default="Семейный бюджет")
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="family")
    goals = relationship("Goal", back_populates="family")

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    language = Column(String, default="ru")
    currency = Column(String, default="AMD")
    
    # Доступы и статусы
    is_admin = Column(Boolean, default=False)
    is_blocked = Column(Boolean, default=False)
    is_premium = Column(Boolean, default=False)
    bot_active = Column(Boolean, default=True)
    
    # 3-дневный триал и премиум
    trial_until = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=3))
    premium_until = Column(DateTime, nullable=True)

    # Стрики (Серия дней ведения)
    streak_count = Column(Integer, default=1)
    last_streak_date = Column(Date, nullable=True)

    # Связь с семьей
    family_id = Column(Integer, ForeignKey("families.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)

    records = relationship("Record", back_populates="user")
    family = relationship("Family", back_populates="users")
    goals = relationship("Goal", back_populates="user")

class Goal(Base):
    __tablename__ = "goals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    family_id = Column(Integer, ForeignKey("families.id"), nullable=True)
    title = Column(String)
    target_amount = Column(Float, default=0.0)
    current_amount = Column(Float, default=0.0)
    currency = Column(String, default="AMD")
    icon = Column(String, default="🎯")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="goals")
    family = relationship("Family", back_populates="goals")

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

    # Напоминания и дедлайны
    status = Column(String, default="pending")         # 'pending' или 'completed'
    due_date = Column(DateTime, nullable=True)         # Дата и время дедлайна
    is_recurring = Column(Boolean, default=False)      # Повторяющийся платеж
    recurrence_rule = Column(String, nullable=True)    # 'monthly', 'weekly', 'daily'
    is_reminded = Column(Boolean, default=False)       # Статус отправки напоминания

    user = relationship("User", back_populates="records")