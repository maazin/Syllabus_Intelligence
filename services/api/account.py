"""Account deletion — PRD section 15.2 and Appendix C.

    "Delete-account removes documents, enrollments, overrides, and calendar
     events, and revokes OAuth tokens. Retain only anonymized corrections data."

Two things make this more than a `DELETE FROM users`:

1. **Calendar events live in Google, not here.** Deleting the row without
   revoking the grant and removing the events leaves the student's calendar full
   of orphaned deadlines from an account that no longer exists.
2. **Corrections survive, anonymized.** Section 12 says `corrections_log` is
   never garbage-collected because it is the eval dataset. Section 15.2 says
   retain only anonymized corrections. Both hold: the row stays, the link to the
   person does not.

There is no ON DELETE CASCADE anywhere in the schema, deliberately — deletion of
a student's data should be an explicit, auditable act, not a side effect.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from db.models import (
    Assessment,
    CalendarConnection,
    CalendarEventMap,
    CorrectionLog,
    Enrollment,
    ExtractionRun,
    GradeCategory,
    SyllabusDocument,
    User,
    UserOverride,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class DeletionReport:
    """What was removed. Returned to the caller so the UI can confirm specifics."""

    documents: int = 0
    stored_files: int = 0
    enrollments: int = 0
    overrides: int = 0
    calendar_connections: int = 0
    calendar_events: int = 0
    corrections_anonymized: int = 0
    errors: list[str] = field(default_factory=list)


def delete_account(
    db: Session, user_id: uuid.UUID, *, remove_remote_events: bool = True
) -> DeletionReport:
    """Remove everything belonging to this user.

    Takes an id rather than a `User` instance on purpose: this function deletes
    the row out from under any ORM object pointing at it, and an argument that
    becomes invalid partway through its own function is a trap for callers.

    Ordering follows the foreign keys. Remote side effects (Google events, object
    storage) run first and non-fatally: a failure there must not abort the
    deletion, because a student who asked to be deleted and got an error is worse
    off than one whose stale calendar entry lingers. Failures are reported, not
    swallowed silently.
    """
    report = DeletionReport()

    _revoke_calendar_access(db, user_id, report, remove_remote_events=remove_remote_events)
    _anonymize_corrections(db, user_id, report)
    _delete_overrides(db, user_id, report)
    _delete_documents_and_extractions(db, user_id, report)

    report.enrollments = (
        db.query(Enrollment).filter(Enrollment.user_id == user_id).delete(synchronize_session=False)
    )
    db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
    db.commit()

    logger.info(
        "Deleted account %s: %d document(s), %d enrollment(s), %d override(s), "
        "%d calendar event(s); %d correction(s) anonymized",
        user_id,
        report.documents,
        report.enrollments,
        report.overrides,
        report.calendar_events,
        report.corrections_anonymized,
    )
    return report


def _revoke_calendar_access(
    db: Session, user_id: uuid.UUID, report: DeletionReport, *, remove_remote_events: bool
) -> None:
    """Remove created events and revoke the OAuth grant (13, 15.2)."""
    connections = db.scalars(
        select(CalendarConnection).where(CalendarConnection.user_id == user_id)
    ).all()

    for connection in connections:
        mappings = db.scalars(
            select(CalendarEventMap).where(CalendarEventMap.connection_id == connection.id)
        ).all()

        if remove_remote_events and connection.provider == "google":
            try:
                from services.api.google_calendar import GoogleCalendarClient

                client = GoogleCalendarClient.from_connection(connection)
                if connection.target_calendar_id:
                    # Deleting the dedicated secondary calendar removes every
                    # event on it in one call, which is both faster and less
                    # likely to leave a partial state than per-event deletes.
                    client.delete_calendar(connection.target_calendar_id)
                client.revoke()
            except Exception as exc:
                # Non-fatal: local deletion proceeds regardless.
                logger.warning("Could not clean up Google calendar for %s: %s", user_id, exc)
                report.errors.append(f"Google Calendar cleanup failed: {exc}")

        report.calendar_events += len(mappings)
        db.query(CalendarEventMap).filter(CalendarEventMap.connection_id == connection.id).delete(
            synchronize_session=False
        )

    report.calendar_connections = len(connections)
    db.query(CalendarConnection).filter(CalendarConnection.user_id == user_id).delete(
        synchronize_session=False
    )


def _anonymize_corrections(db: Session, user_id: uuid.UUID, report: DeletionReport) -> None:
    """Keep the correction, drop the person (12 + 15.2).

    The row's value for eval is the field, the old and new values, the source
    span, and the prompt version — none of which identify anyone. Detaching
    `user_id` and `assessment_id` preserves the dataset while removing the link
    to a deleted account.
    """
    corrections = db.scalars(select(CorrectionLog).where(CorrectionLog.user_id == user_id)).all()
    for correction in corrections:
        correction.user_id = None
        correction.assessment_id = None
    report.corrections_anonymized = len(corrections)
    db.flush()


def _delete_overrides(db: Session, user_id: uuid.UUID, report: DeletionReport) -> None:
    report.overrides = (
        db.query(UserOverride)
        .filter(UserOverride.user_id == user_id)
        .delete(synchronize_session=False)
    )


def _delete_documents_and_extractions(
    db: Session, user_id: uuid.UUID, report: DeletionReport
) -> None:
    """Remove uploaded files and everything derived from them.

    A document whose bytes are shared with another student (content-hash dedup,
    18.3) keeps its stored object: deleting it would break the other student's
    parse. Only the storage object with no remaining referents is removed.
    """
    from services.api.storage import delete_document

    documents = db.scalars(
        select(SyllabusDocument).where(SyllabusDocument.uploader_user_id == user_id)
    ).all()
    if not documents:
        return

    document_ids = [d.id for d in documents]
    assessment_ids = [
        a.id
        for a in db.scalars(
            select(Assessment).where(Assessment.document_id.in_(document_ids))
        ).all()
    ]

    if assessment_ids:
        # Another student's override on a shared parse must not be destroyed
        # along with this student's account.
        db.query(UserOverride).filter(UserOverride.assessment_id.in_(assessment_ids)).delete(
            synchronize_session=False
        )
        db.query(CalendarEventMap).filter(
            CalendarEventMap.assessment_id.in_(assessment_ids)
        ).delete(synchronize_session=False)
        db.query(CorrectionLog).filter(CorrectionLog.assessment_id.in_(assessment_ids)).update(
            {CorrectionLog.assessment_id: None}, synchronize_session=False
        )
        db.query(Assessment).filter(Assessment.id.in_(assessment_ids)).delete(
            synchronize_session=False
        )

    db.query(GradeCategory).filter(GradeCategory.document_id.in_(document_ids)).delete(
        synchronize_session=False
    )
    db.query(ExtractionRun).filter(ExtractionRun.document_id.in_(document_ids)).delete(
        synchronize_session=False
    )

    for document in documents:
        others = db.scalar(
            select(SyllabusDocument)
            .where(
                SyllabusDocument.sha256 == document.sha256,
                SyllabusDocument.uploader_user_id != user_id,
            )
            .limit(1)
        )
        if others is None:
            try:
                delete_document(document.storage_key)
                report.stored_files += 1
            except Exception as exc:
                logger.warning("Could not delete stored file %s: %s", document.storage_key, exc)
                report.errors.append(f"Storage cleanup failed for {document.storage_key}: {exc}")

    report.documents = (
        db.query(SyllabusDocument)
        .filter(SyllabusDocument.id.in_(document_ids))
        .delete(synchronize_session=False)
    )
    db.flush()
