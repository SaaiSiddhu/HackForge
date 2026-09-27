import logging
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from .config import get_settings

logger = logging.getLogger("hackforge.database")


class Base(DeclarativeBase):
    """Base model for HackForge SQLAlchemy entities."""
    pass


def build_engine(database_url: str | None = None):
    """Construct an SQLAlchemy engine according to validated settings."""
    settings = get_settings()
    url = database_url or settings.database_url

    if url.startswith("sqlite"):
        if settings.environment != "testing":
            raise RuntimeError(
                f"SQLite URL rejected. SQLite is restricted to ENVIRONMENT=testing."
            )
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    # PostgreSQL configuration
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
    )


# Authoritative application engine and session maker
engine = build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a managed database session with rollback on exception.
    
    Transactions must be explicitly managed and committed by caller/service handlers.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_health() -> bool:
    """Execute a simple query to verify database connectivity without leaking credentials."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("Database health check probe failed (database unreachable or unhealthy)")
        return False
