"""Replaceable FastAPI dependencies for the Atlas read-only API."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.delivery.repository import DeliveryRepository, SqlAlchemyDeliveryRepository
from app.search.repository import SqlAlchemySearchRepository
from app.search.service import SearchRepository


def get_session() -> Generator[Session, None, None]:
    """Create and close exactly one SQLAlchemy Session for one request."""

    # Import lazily: importing the ASGI app remains configuration-side-effect free.
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_search_repository(session: Session = Depends(get_session)) -> SearchRepository:
    return SqlAlchemySearchRepository(session)


def get_delivery_repository(session: Session = Depends(get_session)) -> DeliveryRepository:
    return SqlAlchemyDeliveryRepository(session)
