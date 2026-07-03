"""
shared/db.py
Async SQLAlchemy engine, session factory, and dependency injection helper.
Used by all services that need database access.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from shared.config import get_settings
from shared.models import Base

logger = logging.getLogger(__name__)


def _build_engine(database_url: str) -> AsyncEngine:
    """Create async SQLAlchemy engine with connection pool tuned for Supabase free tier."""
    return create_async_engine(
        database_url,
        echo=get_settings().is_development,       # Log SQL in dev
        pool_size=5,                               # Keep small for free tier
        max_overflow=10,
        pool_pre_ping=True,                        # Detect stale connections
        pool_recycle=1800,                         # Recycle after 30 min
        connect_args={
            "statement_cache_size": 0,             # Required for pgBouncer (Supabase pooler)
            "prepared_statement_cache_size": 0,
        },
    )


# Module-level engine and session factory (lazy-initialised)
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = _build_engine(settings.database_url)
        logger.info("Database engine created: %s", settings.supabase_url or "local")
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _async_session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for getting a DB session.
    Use in background tasks and non-FastAPI contexts:

        async with get_db_session() as session:
            result = await session.execute(...)
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency injection for database sessions.
    Use in route handlers:

        @app.get("/")
        async def route(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with get_db_session() as session:
        yield session


async def create_tables() -> None:
    """
    Create all tables defined in Base.metadata.
    Called on app startup in development only.
    In production, use migrations/init_db.py instead.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("All tables created (or already exist).")


async def dispose_engine() -> None:
    """Cleanly shut down the engine connection pool. Call on app shutdown."""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None
        logger.info("Database engine disposed.")
