"""Synchronous SQLAlchemy engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.settings import get_settings


engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_size=3,
    max_overflow=2,
)

SessionLocal = sessionmaker[Session](bind=engine, expire_on_commit=False)
