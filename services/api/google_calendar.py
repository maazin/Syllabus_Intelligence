"""Google Calendar sync — PRD section 13 (US-7).

Written against the REST API directly rather than `google-api-python-client`,
which pulls a large dependency tree for four endpoints.

The rules from section 13, and why each one exists:

- **Never write to the user's primary calendar.** Everything goes on a dedicated
  secondary calendar named for the term, which the student can toggle off or
  delete without touching their own events.
- **Scope is `calendar.app.created`**, so the grant covers only calendars this
  app made. Asking for the student's whole calendar to write five deadlines is
  the kind of thing that makes a security review go badly.
- **A 404 on update means the user deleted the event.** Treat that as intent to
  dismiss and stop recreating it. The PRD is blunt: recreating an event the user
  deleted is the fastest way to get uninstalled.
- **`tbd` is never written; `week_only` becomes an all-day span.** Same 9.3
  invariant that governs every other surface — no fabricated specificity.

Token refresh happens transparently. Refresh tokens are stored encrypted; see
`_decrypt` for the current (deliberately minimal) local-dev behavior and what
production needs instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from db.models import CalendarConnection

from services.api import token_vault

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

#: Section 13: the narrowest scope that covers the need. It grants access only
#: to calendars this application created, never the student's own.
SCOPE = "https://www.googleapis.com/auth/calendar.app.created"


class GoogleCalendarError(Exception):
    pass


class EventDeletedByUser(Exception):
    """Raised when an event 404s on update — the student removed it themselves.

    Callers set `dismissed = true` locally rather than recreating it (13).
    """


@dataclass
class CalendarEvent:
    """One assessment, shaped for the Google Calendar API."""

    assessment_id: str
    summary: str
    description: str
    #: Exactly one of these three is set, mirroring `due_precision` (9.3).
    start_at: datetime | None = None
    all_day_on: date | None = None
    all_day_span: tuple[date, date] | None = None

    def to_payload(self) -> dict:
        body: dict = {
            "summary": self.summary,
            "description": self.description,
            "source": {"title": "Syllabus Intelligence", "url": _app_url()},
        }
        if self.start_at is not None:
            # Google wants an end, and a deadline has no duration. An hour is the
            # smallest block that still shows up as a readable event rather than
            # a hairline in a week view.
            body["start"] = {"dateTime": self.start_at.isoformat()}
            body["end"] = {"dateTime": (self.start_at + timedelta(hours=1)).isoformat()}
        elif self.all_day_span is not None:
            span_start, span_end = self.all_day_span
            body["start"] = {"date": span_start.isoformat()}
            body["end"] = {"date": span_end.isoformat()}
        elif self.all_day_on is not None:
            body["start"] = {"date": self.all_day_on.isoformat()}
            body["end"] = {"date": (self.all_day_on + timedelta(days=1)).isoformat()}
        else:
            raise ValueError(f"{self.assessment_id} has no date; tbd items are never written (9.3)")
        return body


def _app_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:4200")


def _client_id() -> str:
    value = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID")
    if not value:
        raise GoogleCalendarError("GOOGLE_CALENDAR_CLIENT_ID is not configured")
    return value


def _client_secret() -> str:
    value = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET")
    if not value:
        raise GoogleCalendarError("GOOGLE_CALENDAR_CLIENT_SECRET is not configured")
    return value


def encrypt_token(token: str) -> str:
    """Seal a refresh token for storage. See `token_vault` for the why."""
    return token_vault.seal(token)


def _decrypt(token: str) -> str:
    try:
        return token_vault.open_sealed(token)
    except token_vault.TokenVaultError as exc:
        raise GoogleCalendarError(str(exc)) from exc


class GoogleCalendarClient:
    """Thin wrapper over the four Calendar endpoints this product uses."""

    def __init__(self, refresh_token: str, *, http: httpx.Client | None = None) -> None:
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._http = http or httpx.Client(timeout=20.0)

    @classmethod
    def from_connection(
        cls, connection: CalendarConnection, *, http: httpx.Client | None = None
    ) -> GoogleCalendarClient:
        if not connection.refresh_token_enc:
            raise GoogleCalendarError("Connection has no stored refresh token")
        return cls(_decrypt(connection.refresh_token_enc), http=http)

    # --- auth ---------------------------------------------------------------------

    def _token(self) -> str:
        if self._access_token and self._expires_at and datetime.now(UTC) < self._expires_at:
            return self._access_token

        response = self._http.post(
            TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code != 200:
            raise GoogleCalendarError(
                f"Token refresh failed ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        self._access_token = payload["access_token"]
        # Refresh a minute early so a request never races the expiry.
        self._expires_at = datetime.now(UTC) + timedelta(
            seconds=payload.get("expires_in", 3600) - 60
        )
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def revoke(self) -> None:
        """Revoke the grant entirely (15.2's delete-account requirement)."""
        response = self._http.post(REVOKE_URL, data={"token": self._refresh_token})
        # 400 means the token was already invalid, which is the desired end state.
        if response.status_code not in (200, 400):
            raise GoogleCalendarError(f"Revoke failed ({response.status_code})")

    # --- calendars ----------------------------------------------------------------

    def create_calendar(self, name: str, timezone: str) -> str:
        """Create the dedicated secondary calendar. Never the primary (13)."""
        response = self._http.post(
            f"{CALENDAR_API}/calendars",
            headers=self._headers(),
            json={"summary": name, "timeZone": timezone},
        )
        if response.status_code not in (200, 201):
            raise GoogleCalendarError(
                f"Could not create calendar ({response.status_code}): {response.text[:200]}"
            )
        return response.json()["id"]

    def delete_calendar(self, calendar_id: str) -> None:
        """Remove the created calendar and everything on it.

        Guarded against `primary` explicitly. The scope should already make this
        impossible, but the consequence of getting it wrong — deleting a
        student's entire calendar — is severe enough to check twice.
        """
        if calendar_id in ("primary", "", None):
            raise GoogleCalendarError("Refusing to delete the user's primary calendar")
        response = self._http.delete(
            f"{CALENDAR_API}/calendars/{calendar_id}", headers=self._headers()
        )
        if response.status_code not in (200, 204, 404, 410):
            raise GoogleCalendarError(f"Could not delete calendar ({response.status_code})")

    # --- events -------------------------------------------------------------------

    def insert_event(self, calendar_id: str, event: CalendarEvent) -> tuple[str, str | None]:
        response = self._http.post(
            f"{CALENDAR_API}/calendars/{calendar_id}/events",
            headers=self._headers(),
            json=event.to_payload(),
        )
        if response.status_code not in (200, 201):
            raise GoogleCalendarError(
                f"Could not create event ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        return payload["id"], payload.get("etag")

    def update_event(
        self, calendar_id: str, external_event_id: str, event: CalendarEvent
    ) -> tuple[str, str | None]:
        """Update an event, or report that the student deleted it.

        A 404 or 410 here is not an error to retry: it means the event is gone
        because the student removed it. Recreating it is the fastest way to get
        uninstalled (13), so this raises a distinct exception the caller turns
        into a local dismissal.
        """
        response = self._http.put(
            f"{CALENDAR_API}/calendars/{calendar_id}/events/{external_event_id}",
            headers=self._headers(),
            json=event.to_payload(),
        )
        if response.status_code in (404, 410):
            raise EventDeletedByUser(external_event_id)
        if response.status_code != 200:
            raise GoogleCalendarError(
                f"Could not update event ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        return payload["id"], payload.get("etag")

    def delete_event(self, calendar_id: str, external_event_id: str) -> None:
        response = self._http.delete(
            f"{CALENDAR_API}/calendars/{calendar_id}/events/{external_event_id}",
            headers=self._headers(),
        )
        # Already gone is success.
        if response.status_code not in (200, 204, 404, 410):
            raise GoogleCalendarError(f"Could not delete event ({response.status_code})")


# --- OAuth flow helpers -------------------------------------------------------------


def authorization_url(state: str, redirect_uri: str) -> str:
    """The consent URL. `access_type=offline` is what yields a refresh token."""
    from urllib.parse import urlencode

    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Without this, Google omits the refresh token on re-consent, and the
        # connection silently becomes unrenewable after the first hour.
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str, *, http: httpx.Client | None = None) -> dict:
    """Trade the callback code for tokens."""
    client = http or httpx.Client(timeout=20.0)
    response = client.post(
        TOKEN_URL,
        data={
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if response.status_code != 200:
        raise GoogleCalendarError(
            f"Code exchange failed ({response.status_code}): {response.text[:200]}"
        )
    payload = response.json()
    if "refresh_token" not in payload:
        raise GoogleCalendarError(
            "Google returned no refresh token; the grant cannot be renewed. "
            "This usually means prompt=consent was omitted on re-authorization."
        )
    return payload
