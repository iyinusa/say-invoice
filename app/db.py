from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.settings import get_settings


class Base(DeclarativeBase):
    # Named constraints, so later migrations can alter them (SQLite batch mode needs names).
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> Any:
    settings = get_settings()
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in settings.database_url:
            # One shared connection, or every session would see its own empty database.
            kwargs["poolclass"] = StaticPool
    return create_async_engine(settings.database_url, **kwargs)


def get_engine() -> Any:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def reset_engine_for_tests() -> None:
    """Test hook: drop cached engine/sessionmaker so a new DATABASE_URL takes effect."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
