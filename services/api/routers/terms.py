"""Term lookup.

The client needs a term id to ask for a heatmap, and it has no other way to
learn one. Without this the heatmap can only ever render its empty state,
which is what happens when an API is designed endpoint by endpoint without
walking the screen that consumes it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from db.models import Institution, Term, User
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.deps import current_user, get_db

router = APIRouter(tags=["terms"])


class TermOut(BaseModel):
    id: str
    name: str
    start_date: str
    end_date: str
    finals_start: str | None
    finals_end: str | None
    is_current: bool


def _to_out(term: Term, today: date) -> TermOut:
    last_day = term.finals_end or term.end_date
    return TermOut(
        id=str(term.id),
        name=term.name,
        start_date=term.start_date.isoformat(),
        end_date=term.end_date.isoformat(),
        finals_start=term.finals_start.isoformat() if term.finals_start else None,
        finals_end=term.finals_end.isoformat() if term.finals_end else None,
        is_current=term.start_date <= today <= last_day,
    )


@router.get("/terms", response_model=list[TermOut])
def list_terms(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[TermOut]:
    """Every term for the student's institution, most recent first."""
    today = datetime.now(UTC).date()
    terms = db.scalars(
        select(Term)
        .join(Institution, Term.institution_id == Institution.id)
        .where(Term.institution_id == user.institution_id)
        .order_by(Term.start_date.desc())
    ).all()
    return [_to_out(term, today) for term in terms]


@router.get("/terms/current", response_model=TermOut | None)
def current_term(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> TermOut | None:
    """The term the student is in right now.

    Between terms there is no current term, so this falls back to the next one
    that starts. A student setting up in the week before classes begin is the
    product's single busiest moment (section 4), and returning nothing then
    would leave them looking at an empty heatmap.
    """
    today = datetime.now(UTC).date()
    terms = db.scalars(
        select(Term)
        .where(Term.institution_id == user.institution_id)
        .order_by(Term.start_date.asc())
    ).all()
    if not terms:
        return None

    for term in terms:
        if term.start_date <= today <= (term.finals_end or term.end_date):
            return _to_out(term, today)

    upcoming = [term for term in terms if term.start_date > today]
    if upcoming:
        return _to_out(upcoming[0], today)

    # Every term is in the past: the most recent one is still the useful answer.
    return _to_out(terms[-1], today)
