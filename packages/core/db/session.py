from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


#: Schemes a managed Postgres hands out, and the driver this project actually
#: installs. Neon, Supabase, Render and Heroku all emit one of these, and
#: SQLAlchemy resolves a bare `postgresql://` to psycopg2, which is not in any
#: requirements file here. Without this the API imports fine locally, where
#: `.env` spells the driver out, and dies on its first production boot with
#: `ModuleNotFoundError: psycopg2`.
_DRIVER = "postgresql+psycopg"
_BARE_SCHEMES = ("postgresql://", "postgres://")


def normalize_database_url(url: str) -> str:
    """Force the psycopg 3 driver onto a connection string.

    A URL that already names a driver is left alone, including a deliberate
    `postgresql+asyncpg://`, so this cannot quietly override a choice someone
    made on purpose.
    """
    for scheme in _BARE_SCHEMES:
        if url.startswith(scheme):
            return f"{_DRIVER}://{url[len(scheme) :]}"
    return url


def _database_url() -> str:
    return normalize_database_url(
        os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@localhost:5432/syllint",
        )
    )


# `pool_pre_ping` matters more here than it looks. Neon suspends compute after
# five minutes idle, so the first request after a quiet spell meets a pool full
# of connections the server has already dropped. Without the ping that surfaces
# to a student as a failed page load rather than a slow one.
engine = create_engine(_database_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
