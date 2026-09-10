"""Connection-string handling.

These are cheap tests for an expensive failure: the wrong driver in a URL is
invisible until the first deploy, and it fails at import time, which means the
container never becomes healthy and Cloud Run rolls back to a revision that
looks fine.
"""

from __future__ import annotations

import pytest
from db.session import normalize_database_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # What Neon actually returns from its API.
        (
            "postgresql://syllint_app:pw@ep-cool-1.us-east-2.aws.neon.tech/syllint?sslmode=require",
            "postgresql+psycopg://syllint_app:pw@ep-cool-1.us-east-2.aws.neon.tech/syllint?sslmode=require",
        ),
        # The older spelling several providers still emit.
        (
            "postgres://user:pw@host:5432/db",
            "postgresql+psycopg://user:pw@host:5432/db",
        ),
    ],
)
def test_bare_schemes_get_the_installed_driver(given: str, expected: str) -> None:
    assert normalize_database_url(given) == expected


def test_an_explicit_driver_is_left_alone() -> None:
    """Someone who names a driver meant it, including a driver we do not use."""
    for url in (
        "postgresql+psycopg://user:pw@host/db",
        "postgresql+asyncpg://user:pw@host/db",
        "sqlite:///./local.db",
    ):
        assert normalize_database_url(url) == url


def test_a_password_containing_the_scheme_is_not_mangled() -> None:
    """Only the leading scheme is replaced, never a later match."""
    url = "postgres://user:postgres%3A%2F%2Fpw@host/db"
    assert normalize_database_url(url) == "postgresql+psycopg://user:postgres%3A%2F%2Fpw@host/db"
