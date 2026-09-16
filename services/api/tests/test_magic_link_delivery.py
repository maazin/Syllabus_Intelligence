"""Magic-link delivery (PRD sections 15.2 and 18.4).

Sign-in is the email. MailHog accepts anything on a plain socket, so the
authenticated, STARTTLS path that every real relay requires is exactly the
path that never gets exercised locally. These tests pin it down with a
recording stand-in for smtplib.
"""

from __future__ import annotations

import smtplib

import pytest

from services.api.routers import auth


class _RecordingSMTP:
    calls: list[tuple[str, tuple]] = []
    fail_login = False

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.calls.append(("connect", (host, port)))

    def __enter__(self) -> _RecordingSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append(("quit", ()))

    def starttls(self) -> None:
        self.calls.append(("starttls", ()))

    def ehlo(self) -> None:
        self.calls.append(("ehlo", ()))

    def login(self, user: str, password: str) -> None:
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(("login", (user, password)))

    def send_message(self, message: object) -> None:
        self.calls.append(("send", (message["To"],)))


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingSMTP]:
    _RecordingSMTP.calls = []
    _RecordingSMTP.fail_login = False
    monkeypatch.setattr(auth.smtplib, "SMTP", _RecordingSMTP)
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "relay.example")
    monkeypatch.setenv("SMTP_PORT", "587")
    return _RecordingSMTP


def test_a_local_catcher_gets_a_plain_unauthenticated_send(
    smtp: type[_RecordingSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    auth._send_magic_link("s@test.edu", "tok")
    assert [c for c, _ in smtp.calls] == ["connect", "send", "quit"]


def test_a_real_relay_gets_starttls_then_login_then_send(
    smtp: type[_RecordingSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_USER", "account@example.edu")
    monkeypatch.setenv("SMTP_PASSWORD", "relay-key")
    auth._send_magic_link("s@test.edu", "tok")
    names = [c for c, _ in smtp.calls]
    assert names == ["connect", "starttls", "ehlo", "login", "send", "quit"]
    assert ("login", ("account@example.edu", "relay-key")) in smtp.calls
    # TLS is negotiated before the credential goes over the wire.
    assert names.index("starttls") < names.index("login")


def test_login_defaults_to_the_from_address(
    smtp: type[_RecordingSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brevo authenticates with the account email, which is also the sender."""
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.setenv("SMTP_PASSWORD", "relay-key")
    monkeypatch.setenv("EMAIL_FROM", "no-reply@example.edu")
    auth._send_magic_link("s@test.edu", "tok")
    assert ("login", ("no-reply@example.edu", "relay-key")) in smtp.calls


def test_a_rejected_login_is_a_503_not_a_500(
    smtp: type[_RecordingSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong relay password is an outage, and it should read as one."""
    monkeypatch.setenv("SMTP_PASSWORD", "wrong")
    smtp.fail_login = True
    with pytest.raises(auth.HTTPException) as exc:
        auth._send_magic_link("s@test.edu", "tok")
    assert exc.value.status_code == 503
