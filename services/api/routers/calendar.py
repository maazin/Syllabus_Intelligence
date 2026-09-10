"""Calendar sync — PRD section 13 (US-7).

The ICS feed is built first and deliberately: it works everywhere with no OAuth,
no token refresh, and no support burden. Google Calendar is the primary sync but
depends on OAuth verification that takes weeks to obtain (section 19), so ICS is
the always-available fallback.

The rules that keep this from getting the product uninstalled:
  - never write to the user's primary calendar
  - `tbd` items are not written at all; `week_only` becomes an all-day span
  - a 404 on update means the user deleted the event — respect that, never recreate
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

from db.localtime import DEFAULT_TIMEZONE, local_datetime
from db.models import Assessment, Course, Enrollment, Institution, Section, User, UserOverride
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.deps import current_user, get_db

router = APIRouter(tags=["calendar"])

PRODID = "-//Syllabus Intelligence//EN"


def _feed_secret() -> str:
    return os.environ.get("JWT_SECRET", "local-dev-secret-not-for-production")


def feed_token(user_id: uuid.UUID) -> str:
    """An unguessable, signed, per-user token (13).

    HMAC rather than a random column so the feed URL can be regenerated without
    a migration, and constant-time compared on the way back in.
    """
    return hmac.new(_feed_secret().encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


def _escape(text: str) -> str:
    """Escape a value for an RFC 5545 TEXT property.

    Backslash first — escaping it after the others would double-escape the
    backslashes this function just introduced.
    """
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 caps lines at 75 octets; continuations start with a space."""
    if len(line) <= 75:
        return line
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)


@router.get("/calendar/ics/{token}")
def ics_feed(token: str, db: Session = Depends(get_db)) -> Response:
    """Read-only ICS feed. Signed URL in the path, no bearer token — calendar
    clients cannot send headers."""
    user = None
    for candidate in db.scalars(select(User)).all():
        if hmac.compare_digest(feed_token(candidate.id), token):
            user = candidate
            break
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feed not found")

    institution = db.scalar(select(Institution).where(Institution.id == user.institution_id))
    timezone = institution.timezone if institution else DEFAULT_TIMEZONE

    rows = db.execute(
        select(Assessment, Course, UserOverride)
        .join(Section, Assessment.section_id == Section.id)
        .join(Course, Section.course_id == Course.id)
        .join(
            Enrollment,
            (Enrollment.section_id == Section.id) & (Enrollment.user_id == user.id),
        )
        .outerjoin(
            UserOverride,
            (UserOverride.assessment_id == Assessment.id) & (UserOverride.user_id == user.id),
        )
    ).all()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Coursework",
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    for assessment, course, override in rows:
        if override is not None and override.dismissed:
            continue
        # 9.3 / 13: tbd is never written to a calendar at all.
        if assessment.due_precision == "tbd":
            continue
        # Nothing reaches a real calendar before review is complete (US-3).
        if assessment.verified_at is None:
            continue

        stored_due = override.due_at if override and override.due_at else assessment.due_at
        # Convert out of stored UTC before deriving a calendar day: an 11:59pm
        # local deadline is 03:59 UTC the next morning, and an all-day event
        # built from the UTC day lands on the wrong date (see db.localtime).
        due_at = local_datetime(stored_due, timezone)
        if due_at is None:
            continue

        title = override.title if override and override.title else assessment.title
        course_label = f"{course.subject_code} {course.catalog_number}"
        weight = f"{assessment.weight_pct:g}% of grade" if assessment.weight_pct else "—"

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{assessment.id}@syllabus-intelligence")
        lines.append(f"DTSTAMP:{stamp}")

        if assessment.due_precision == "exact_datetime" and not assessment.time_inferred:
            # A stated time is a real instant, so it is written in UTC with the
            # trailing Z rather than as a floating local time.
            utc = due_at.astimezone(UTC)
            lines.append(f"DTSTART:{utc.strftime('%Y%m%dT%H%M%SZ')}")
            lines.append(f"DTEND:{(utc + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}")
        elif assessment.due_precision == "week_only":
            # An all-day event spanning the week — never a specific day (13).
            start = due_at.date() - timedelta(days=due_at.weekday())
            lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(start + timedelta(days=7)).strftime('%Y%m%d')}")
        else:
            day = due_at.date()
            lines.append(f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}")

        lines.append(_fold(f"SUMMARY:{_escape(f'{course_label}: {title}')}"))
        description = (
            f"{course_label} — {assessment.type.replace('_', ' ')}, {weight}."
            f"\\n\\nFrom the syllabus: {assessment.source_span}"
        )
        if assessment.time_inferred:
            description += "\\n\\nTime not stated in the syllabus; shown at the default due time."
        lines.append(_fold(f"DESCRIPTION:{_escape(description)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"

    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="coursework.ics"'},
    )


@router.get("/calendar/ics-url")
def my_feed_url(user: User = Depends(current_user)) -> dict:
    base = os.environ.get("API_BASE_URL", "http://localhost:8100")
    return {"url": f"{base}/api/v1/calendar/ics/{feed_token(user.id)}"}


# --- Google Calendar (section 13) ---------------------------------------------------


class GoogleConnectStart(BaseModel):
    authorization_url: str


class GoogleCallback(BaseModel):
    code: str
    state: str | None = None


def _redirect_uri() -> str:
    return os.environ.get(
        "GOOGLE_CALENDAR_REDIRECT_URI", "http://localhost:4200/settings/calendar/callback"
    )


@router.get("/calendar/google/authorize", response_model=GoogleConnectStart)
def google_authorize(user: User = Depends(current_user)) -> GoogleConnectStart:
    """Begin the OAuth grant.

    `state` is the user's own signed feed token, which is unguessable and already
    bound to this user — so the callback can verify the grant came back to the
    same person who started it.
    """
    from services.api.google_calendar import GoogleCalendarError, authorization_url

    try:
        url = authorization_url(state=feed_token(user.id), redirect_uri=_redirect_uri())
    except GoogleCalendarError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return GoogleConnectStart(authorization_url=url)


@router.post("/calendar/google/connect", status_code=status.HTTP_200_OK)
def google_connect(
    payload: GoogleCallback,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Complete the grant and create the dedicated secondary calendar (13)."""
    from db.models import CalendarConnection, Term

    from services.api.google_calendar import (
        GoogleCalendarClient,
        GoogleCalendarError,
        encrypt_token,
        exchange_code,
    )

    if payload.state is not None and not hmac.compare_digest(payload.state, feed_token(user.id)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth state did not match")

    institution = db.scalar(select(Institution).where(Institution.id == user.institution_id))
    timezone = institution.timezone if institution else DEFAULT_TIMEZONE

    try:
        tokens = exchange_code(payload.code, _redirect_uri())
        client = GoogleCalendarClient(tokens["refresh_token"])

        term = db.scalar(
            select(Term)
            .where(Term.institution_id == user.institution_id)
            .order_by(Term.start_date.desc())
        )
        # Named for the term so the student can tell at a glance what it holds
        # and toggle or delete it without touching their own calendars.
        name = f"Coursework — {term.name}" if term else "Coursework"
        calendar_id = client.create_calendar(name, timezone)
    except GoogleCalendarError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    connection = db.scalar(
        select(CalendarConnection).where(
            CalendarConnection.user_id == user.id, CalendarConnection.provider == "google"
        )
    )
    if connection is None:
        connection = CalendarConnection(user_id=user.id, provider="google")
        db.add(connection)
    connection.refresh_token_enc = encrypt_token(tokens["refresh_token"])
    connection.target_calendar_id = calendar_id
    db.commit()

    return {"status": "connected", "calendar_id": calendar_id, "calendar_name": name}


@router.post("/calendar/google/sync", status_code=status.HTTP_200_OK)
def google_sync(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Push reviewed items onto the connected calendar.

    Called on change plus a nightly reconcile (13). Idempotent: a second call
    with nothing changed creates nothing and updates nothing.
    """
    from services.api.calendar_sync import sync_user
    from services.api.google_calendar import GoogleCalendarError

    institution = db.scalar(select(Institution).where(Institution.id == user.institution_id))
    timezone = institution.timezone if institution else DEFAULT_TIMEZONE

    try:
        report = sync_user(db, user, timezone=timezone)
    except GoogleCalendarError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return {
        "created": report.created,
        "updated": report.updated,
        "deleted": report.deleted,
        "dismissed_by_user": report.dismissed_by_user,
        "skipped": report.skipped,
        "errors": report.errors,
    }


@router.delete("/calendar/google", status_code=status.HTTP_200_OK)
def google_disconnect(
    remove_calendar: bool = True,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Disconnect, offering to remove the created calendar (13).

    `remove_calendar` defaults to True but is a parameter because the PRD calls
    for a clear confirmation — the client asks, and passes the answer through.
    """
    from db.models import CalendarConnection, CalendarEventMap

    from services.api.google_calendar import GoogleCalendarClient

    connection = db.scalar(
        select(CalendarConnection).where(
            CalendarConnection.user_id == user.id, CalendarConnection.provider == "google"
        )
    )
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Google Calendar connection")

    removed = False
    if remove_calendar and connection.target_calendar_id:
        try:
            client = GoogleCalendarClient.from_connection(connection)
            client.delete_calendar(connection.target_calendar_id)
            client.revoke()
            removed = True
        except Exception:
            # Local disconnection proceeds regardless; a student asking to
            # disconnect must not be blocked by a Google-side failure.
            logging.getLogger(__name__).exception("Google cleanup failed on disconnect")

    db.query(CalendarEventMap).filter(CalendarEventMap.connection_id == connection.id).delete(
        synchronize_session=False
    )
    db.delete(connection)
    db.commit()

    return {"status": "disconnected", "calendar_removed": removed}
