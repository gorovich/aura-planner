from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    language = Column(String, default="ru")
    is_premium = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    records = relationship("Record", back_populates="user", cascade="all, delete-orphan")

class Record(Base):
    __tablename__ = "records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    category = Column(String, nullable=False)  # task, finance, habit, note
    title = Column(String, nullable=False)
    amount = Column(Float, default=0.0)
    currency = Column(String, default="$")
    is_completed = Column(Boolean, default=False)
    streak = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="records")