"""Alembic environment — PRD section 27 ("one migration per schema change,
never edited after merge").

`DATABASE_URL` comes from the environment rather than alembic.ini so the same
migration set runs against local Postgres and Neon with no file edit.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import db.models  # noqa: E402,F401  (import registers every model on Base.metadata)
from db.session import Base, normalize_database_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Normalized for the same reason the application engine is: a managed provider
# hands out a bare `postgresql://`, and alembic would try to import psycopg2.
# The migration would then fail on the very first production run, before any
# revision reached the database.
#
# The doubled percent is not decoration. Alembic stores this in a ConfigParser
# with interpolation enabled, and a URL-encoded character in a generated
# password (`%2F`, say) raises InterpolationSyntaxError on read. Neon generates
# the password, so which characters land in it is not something this project
# gets to decide.
config.set_main_option(
    "sqlalchemy.url",
    normalize_database_url(
        os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/syllint"
        )
    ).replace("%", "%%"),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
