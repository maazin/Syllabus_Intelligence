"""The MVP loop against the real stack (PRD section 27, US-1 through US-5).

Every other test in this repository stubs something: the Python suite stubs
the queue, the browser suite stubs the API. Each is right to, and together
they leave one gap: the contract between the pieces. A worker that never
registered the task the API sends, a status endpoint the review screen never
polled, a course match nobody performed. All three were real, all three
passed every unit test, and all three surfaced the first time this file ran.

This runs against a live API, worker, database, object store and mail
catcher, with the model replaced by a recorded extraction (LLM_MODE=replay).
It is skipped unless SMOKE_API_URL is set, because it needs the stack up:

    docker compose -f infra/docker/docker-compose.yml up -d
    SMOKE_API_URL=http://localhost:8100 SMOKE_MAILHOG_URL=http://localhost:8125 \\
        pytest tests/smoke -q
"""

from __future__ import annotations

import os
import quopri
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest

API = os.environ.get("SMOKE_API_URL")
MAILHOG = os.environ.get("SMOKE_MAILHOG_URL", "http://localhost:8125")
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "syllabi" / "cs_lecture_native_pdf.pdf"

pytestmark = pytest.mark.skipif(
    not API, reason="SMOKE_API_URL is not set; see the module docstring"
)


def _magic_link_token(email: str, http: httpx.Client) -> str:
    """Pull the token out of the email the API just sent, via MailHog's API."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        messages = http.get(f"{MAILHOG}/api/v2/search", params={"kind": "to", "query": email})
        messages.raise_for_status()
        for item in messages.json().get("items", []):
            # MailHog stores the raw MIME body. smtplib sends long lines as
            # quoted-printable, which soft-wraps a JWT across lines with `=`;
            # a mail client decodes that, and so must this.
            body = quopri.decodestring(item["Content"]["Body"].encode()).decode()
            found = re.search(r"token=([A-Za-z0-9._\-]+)", body)
            if found:
                return found.group(1)
        time.sleep(0.5)
    raise AssertionError(f"No magic link arrived for {email} within 20s")


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    with httpx.Client(timeout=30.0) as client:
        yield client


@pytest.fixture(scope="module")
def session(http: httpx.Client) -> dict[str, str]:
    """Sign in the way a student does: ask for a link, click it."""
    email = f"smoke-{uuid.uuid4().hex[:8]}@test.edu"
    requested = http.post(f"{API}/api/v1/auth/magic-link", json={"email": email})
    assert requested.status_code == 202, requested.text

    token = _magic_link_token(email, http)
    verified = http.post(f"{API}/api/v1/auth/verify", json={"token": token})
    assert verified.status_code == 200, verified.text
    access = verified.json()["access_token"]
    return {"Authorization": f"Bearer {access}", "email": email}


def _auth(session: dict[str, str]) -> dict[str, str]:
    return {"Authorization": session["Authorization"]}


def test_the_whole_loop(http: httpx.Client, session: dict[str, str]) -> None:
    if not FIXTURE.is_file():
        pytest.skip("run scripts/generate_fixture_syllabi.py first")
    headers = _auth(session)

    # Unique bytes per run. Dedup (18.3) is a feature, and it would otherwise
    # hand this run the previous run's parse and prove nothing.
    data = FIXTURE.read_bytes() + f"\n% smoke {uuid.uuid4().hex}".encode()

    # --- US-1: upload --------------------------------------------------------
    uploaded = http.post(
        f"{API}/api/v1/documents",
        headers=headers,
        files=[("files", (FIXTURE.name, data, "application/pdf"))],
    )
    assert uploaded.status_code == 202, uploaded.text
    [accepted] = uploaded.json()
    assert accepted["status"] == "queued" and accepted["document_id"], accepted
    document_id = accepted["document_id"]

    # --- the worker parses, matches the course, and reports back -------------
    # US-1 targets p95 under three minutes end to end. Replay skips the model
    # call, so anything near that here is a real problem, not a slow model.
    deadline = time.monotonic() + 120
    status = "queued"
    while time.monotonic() < deadline:
        polled = http.get(f"{API}/api/v1/documents/{document_id}", headers=headers)
        assert polled.status_code == 200, polled.text
        status = polled.json()["status"]
        if status == "needs_course":
            # The fixture names COP 4530 section 003, which the seed carries
            # once. Being asked means the match regressed, and the candidate
            # list is the most useful thing to show for that.
            raise AssertionError(f"Course match should have been unambiguous: {polled.json()}")
        if status in ("succeeded", "failed", "needs_manual_entry"):
            break
        time.sleep(1)
    assert status == "succeeded", f"parse ended as {status!r}: {polled.json()}"

    # --- US-3: the review screen's data ---------------------------------------
    timeline = http.get(
        f"{API}/api/v1/timeline", headers=headers, params={"document_id": document_id}
    )
    assert timeline.status_code == 200, timeline.text
    items = timeline.json()
    assert len(items) == 9, [i["title"] for i in items]
    assert all(i["course"] == "COP 4530" for i in items)
    # Section 9.3: resolved dates carry their precision, and nothing is a bare
    # fabricated time.
    assert {i["due_precision"] for i in items} <= {
        "exact_datetime",
        "date_only",
        "week_only",
        "tbd",
    }
    dated = [i for i in items if i["due_at"]]
    assert dated, "the recorded extraction has dated items"
    assert all(i["source_span"] for i in items), "every item cites its sentence"

    # --- US-4: one correction survives ----------------------------------------
    target = dated[0]
    patched = http.patch(
        f"{API}/api/v1/assessments/{target['assessment_id']}",
        headers=headers,
        json={"field": "due_at", "value": "2026-10-30T23:59:00"},
    )
    assert patched.status_code == 200, patched.text
    again = http.get(
        f"{API}/api/v1/timeline", headers=headers, params={"document_id": document_id}
    ).json()
    edited = next(i for i in again if i["assessment_id"] == target["assessment_id"])
    assert edited["edited"] is True
    assert edited["due_at"].startswith("2026-10-30")

    # --- US-3: finishing the review is what unlocks the calendar -------------
    done = http.post(f"{API}/api/v1/documents/{document_id}/review-complete", headers=headers)
    assert done.status_code == 200, done.text

    # --- US-5: the merged view and the heatmap ---------------------------------
    everything = http.get(f"{API}/api/v1/timeline", headers=headers)
    assert everything.status_code == 200
    assert len(everything.json()) >= 9

    term = http.get(f"{API}/api/v1/terms/current", headers=headers)
    assert term.status_code == 200 and term.json(), term.text
    heatmap = http.get(
        f"{API}/api/v1/heatmap", headers=headers, params={"term_id": term.json()["id"]}
    )
    assert heatmap.status_code == 200, heatmap.text
    cells = heatmap.json()
    assert cells, "a parsed course produces at least one week of load"
    assert sum(c["effort_hours"] for c in cells) > 0
