import os
from functools import lru_cache
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL não configurada.")
    # Railway/Heroku às vezes fornecem URL com "postgres://", mas o SQLAlchemy usa "postgresql://".
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]
    return database_url


@lru_cache
def get_engine():
    return create_engine(get_database_url(), pool_pre_ping=True)


def get_sessionmaker():
    return sessionmaker(autoflush=False, autocommit=False, bind=get_engine())


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
