"""Shared FastAPI dependencies — DB session, auth, current user."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import jwt
from db.models import User
from db.session import SessionLocal
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

_JWT_ALGORITHM = "HS256"
_ACCESS_TOKEN_TTL = timedelta(days=30)
_MAGIC_LINK_TTL = timedelta(minutes=15)

bearer_scheme = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set")
    if os.environ.get("ENVIRONMENT") == "production" and "local-dev" in secret:
        raise RuntimeError("Refusing to run in production with the local dev JWT secret")
    return secret


def institution_domain() -> str:
    """15.2: signup is restricted to the institution's own domain."""
    return os.environ.get("INSTITUTION_EMAIL_DOMAIN", "test.edu")


def issue_magic_link_token(email: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": email, "purpose": "magic_link", "iat": now, "exp": now + _MAGIC_LINK_TTL},
        _secret(),
        algorithm=_JWT_ALGORITHM,
    )


def issue_access_token(user_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": str(user_id), "purpose": "access", "iat": now, "exp": now + _ACCESS_TOKEN_TTL},
        _secret(),
        algorithm=_JWT_ALGORITHM,
    )


def _decode(token: str, purpose: str) -> dict:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    # A magic-link token must never be accepted as an access token, and vice versa.
    if payload.get("purpose") != purpose:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token is not valid for this operation")
    return payload


def verify_magic_link_token(token: str) -> str:
    return _decode(token, "magic_link")["sub"]


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    payload = _decode(credentials.credentials, "access")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token subject") from exc

    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return user
