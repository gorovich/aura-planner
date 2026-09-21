from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")

engine = create_engine(
    DATABASE_URL,
    pool_size=10,        # Базовый размер пула
    max_overflow=20,     # Допустимый перелив
    pool_pre_ping=True,  # Проверка живых подключений
    pool_recycle=1800    # Автоперезапуск соединений каждые 30 мин
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()