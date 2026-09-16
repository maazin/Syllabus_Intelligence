"""Magic-link auth — PRD sections 23 and 15.2.

`.edu` verification at signup, and the minimum data stored: email,
institution, enrollments, overrides (15.2). No password is ever collected,
which removes a whole class of storage and breach risk.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from db.models import Institution, User
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.deps import (
    get_db,
    institution_domain,
    issue_access_token,
    issue_magic_link_token,
    verify_magic_link_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class MagicLinkRequest(BaseModel):
    email: EmailStr


class VerifyRequest(BaseModel):
    token: str


class UserOut(BaseModel):
    id: str
    email: str
    verified: bool


class TokenResponse(BaseModel):
    access_token: str
    user: UserOut


def _send_magic_link(email: str, token: str) -> None:
    """Deliver the link. Locally this lands in MailHog rather than a real inbox.

    Delivery is the whole auth flow: if magic links land in spam during syllabus
    week, signups fail silently (18.4). SPF/DKIM/DMARC must be configured on the
    sending domain before launch, and tested against a real .edu inbox.
    """
    base_url = os.environ.get("APP_BASE_URL", "http://localhost:4200")
    # The web app serves this path (apps/web/src/app/app.routes.ts). The two
    # sides disagreed on it once, each with a passing test suite, and the
    # result was a link that landed on the sign-in form with no message.
    link = f"{base_url}/auth/verify?token={token}"

    if os.environ.get("EMAIL_PROVIDER", "smtp") != "smtp":
        # Brevo/SES live behind the same call site; the provider is a config
        # switch, not a code branch (18.4).
        logger.warning("Non-SMTP email provider configured but not wired; link: %s", link)
        return

    message = EmailMessage()
    message["Subject"] = "Your sign-in link"
    message["From"] = os.environ.get("EMAIL_FROM", "no-reply@syllabus-intelligence.local")
    message["To"] = email
    message.set_content(
        f"Sign in to Syllabus Intelligence:\n\n{link}\n\nThis link expires in 15 minutes."
    )

    host = os.environ.get("SMTP_HOST", "localhost")
    port = int(os.environ.get("SMTP_PORT", "1125"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            # MailHog locally takes anything on a plain socket. Every real relay
            # (Brevo, SES, Postmark) requires STARTTLS and a login on 587, and a
            # relay that is handed an unauthenticated message either rejects it
            # or, worse, accepts it into a spam-scored queue. Credentials being
            # present is the signal that this is a real relay.
            if password:
                smtp.starttls()
                smtp.ehlo()
                smtp.login(user or message["From"], password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send magic link to %s", email)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not send the sign-in email"
        ) from None


@router.post("/magic-link", status_code=status.HTTP_202_ACCEPTED)
def request_magic_link(payload: MagicLinkRequest, db: Session = Depends(get_db)) -> dict:
    domain = institution_domain()
    if not payload.email.lower().endswith(f"@{domain}"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Sign-up is limited to @{domain} addresses.",
        )

    institution = db.scalar(select(Institution).where(Institution.domain == domain))
    if institution is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "This institution has not been configured yet.",
        )

    _send_magic_link(payload.email, issue_magic_link_token(payload.email.lower()))
    # Always 202, whether or not the address already has an account: a different
    # response would let anyone probe which students have signed up.
    return {"status": "sent"}


@router.post("/verify", response_model=TokenResponse)
def verify(payload: VerifyRequest, db: Session = Depends(get_db)) -> TokenResponse:
    from datetime import UTC, datetime

    email = verify_magic_link_token(payload.token)
    domain = institution_domain()
    institution = db.scalar(select(Institution).where(Institution.domain == domain))
    if institution is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Institution not configured")

    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, institution_id=institution.id)
        db.add(user)
    user.verified_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)

    return TokenResponse(
        access_token=issue_access_token(user.id),
        user=UserOut(id=str(user.id), email=user.email, verified=True),
    )
