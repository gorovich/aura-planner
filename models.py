from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True)
    language = Column(String, default="ru")
    currency = Column(String, default="AMD")  # AMD, USD, RUB

    records = relationship("Record", back_populates="owner")

class Record(Base):
    __tablename__ = "records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    category = Column(String, default="task")  # task, finance
    type = Column(String, default="expense")   # expense, income
    title = Column(String)
    amount = Column(Float, default=0.0)
    currency = Column(String, default="AMD")
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="records")