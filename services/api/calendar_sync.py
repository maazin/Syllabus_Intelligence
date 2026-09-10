"""Reconciling assessments with the student's Google calendar — PRD section 13.

Sync is a diff, not a rewrite: for each eligible assessment, create the event if
it has none, update it if it changed, and leave it alone otherwise. Rewriting
everything on every sync would burn quota and, worse, resurrect events the
student deliberately deleted.

Eligibility is stricter than "has a date", and every condition traces to the PRD:

  - review must be complete for the item (US-3: nothing reaches a real calendar
    before the student has confirmed it)
  - `due_precision` must not be `tbd` (9.3)
  - the item must not be dismissed locally
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from db.localtime import DEFAULT_TIMEZONE, local_datetime
from db.models import (
    Assessment,
    CalendarConnection,
    CalendarEventMap,
    Course,
    Enrollment,
    Section,
    User,
    UserOverride,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.google_calendar import (
    CalendarEvent,
    EventDeletedByUser,
    GoogleCalendarClient,
)

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    dismissed_by_user: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def build_event(
    assessment: Assessment,
    override: UserOverride | None,
    course: Course,
    timezone: str,
) -> CalendarEvent | None:
    """Shape one assessment as a calendar event, or None if it must not be written."""
    if assessment.due_precision == "tbd":
        return None
    if override is not None and override.dismissed:
        return None

    stored_due = override.due_at if override and override.due_at else assessment.due_at
    due_at = local_datetime(stored_due, timezone)
    if due_at is None:
        return None

    title = override.title if override and override.title else assessment.title
    course_label = f"{course.subject_code} {course.catalog_number}"
    weight = f"{assessment.weight_pct:g}% of grade" if assessment.weight_pct else "weight unknown"

    # 13: the event body carries course, type, weight, and a link back to the
    # source snippet, so the student can verify from inside their calendar app
    # without returning to ours.
    description_parts = [
        f"{course_label} — {assessment.type.replace('_', ' ')}, {weight}.",
        "",
        f"From the syllabus: {assessment.source_span}",
    ]
    if assessment.page_ref:
        description_parts.append(f"(page {assessment.page_ref})")
    if assessment.time_inferred:
        description_parts += [
            "",
            "The syllabus did not state a time; this shows at the default due time.",
        ]

    event = CalendarEvent(
        assessment_id=str(assessment.id),
        summary=f"{course_label}: {title}",
        description="\n".join(description_parts),
    )

    if assessment.due_precision == "exact_datetime" and not assessment.time_inferred:
        event.start_at = due_at
    elif assessment.due_precision == "week_only":
        # An all-day span across the week — never collapsed to a day (9.3, 13).
        start = due_at.date() - timedelta(days=due_at.weekday())
        event.all_day_span = (start, start + timedelta(days=7))
    else:
        event.all_day_on = due_at.date()
    return event


def sync_user(
    db: Session,
    user: User,
    *,
    client: GoogleCalendarClient | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> SyncReport:
    """Reconcile every eligible assessment for this user onto their calendar."""
    report = SyncReport()

    connection = db.scalar(
        select(CalendarConnection).where(
            CalendarConnection.user_id == user.id, CalendarConnection.provider == "google"
        )
    )
    if connection is None or not connection.target_calendar_id:
        return report

    api = client or GoogleCalendarClient.from_connection(connection)

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

    existing = {
        m.assessment_id: m
        for m in db.scalars(
            select(CalendarEventMap).where(CalendarEventMap.connection_id == connection.id)
        ).all()
    }

    for assessment, course, override in rows:
        mapping = existing.pop(assessment.id, None)

        # US-3: nothing reaches a real calendar until review is complete.
        if assessment.verified_at is None:
            report.skipped += 1
            continue

        event = build_event(assessment, override, course, timezone)

        if event is None:
            # No longer eligible (dismissed, or became tbd on a re-parse).
            if mapping is not None:
                _remove(db, api, connection, mapping, report)
            else:
                report.skipped += 1
            continue

        try:
            if mapping is None:
                event_id, etag = api.insert_event(connection.target_calendar_id, event)
                db.add(
                    CalendarEventMap(
                        connection_id=connection.id,
                        assessment_id=assessment.id,
                        external_event_id=event_id,
                        etag=etag,
                    )
                )
                report.created += 1
            else:
                _, etag = api.update_event(
                    connection.target_calendar_id, mapping.external_event_id, event
                )
                mapping.etag = etag
                report.updated += 1
        except EventDeletedByUser:
            # 13: the student deleted this event. That is intent to dismiss, not
            # a sync error — record it locally and never recreate the event.
            logger.info("Event for assessment %s was deleted in Google; dismissing", assessment.id)
            _dismiss_locally(db, user, assessment, mapping)
            report.dismissed_by_user += 1
        except Exception as exc:
            logger.exception("Sync failed for assessment %s", assessment.id)
            report.errors.append(f"{assessment.title}: {exc}")

    # Anything still mapped has no corresponding assessment any more.
    for orphan in existing.values():
        _remove(db, api, connection, orphan, report)

    from datetime import UTC, datetime

    connection.last_synced_at = datetime.now(UTC)
    db.commit()
    return report


def _remove(
    db: Session,
    api: GoogleCalendarClient,
    connection: CalendarConnection,
    mapping: CalendarEventMap,
    report: SyncReport,
) -> None:
    calendar_id = connection.target_calendar_id
    if calendar_id is None:
        # A connection that was never pointed at a calendar cannot have written
        # this event, so there is nothing remote to remove. The local mapping is
        # still cleared below, which is what stops it being retried forever.
        logger.warning(
            "Connection %s has no target calendar; dropping mapping %s locally",
            connection.id,
            mapping.external_event_id,
        )
        db.delete(mapping)
        report.deleted += 1
        return

    try:
        api.delete_event(calendar_id, mapping.external_event_id)
    except Exception as exc:
        logger.warning("Could not delete event %s: %s", mapping.external_event_id, exc)
        report.errors.append(f"delete {mapping.external_event_id}: {exc}")
    db.delete(mapping)
    report.deleted += 1


def _dismiss_locally(
    db: Session, user: User, assessment: Assessment, mapping: CalendarEventMap | None
) -> None:
    """Record the student's deletion as a dismissal (13).

    The dismissal lands in `user_overrides`, which is the same place every other
    personal edit lives — so a re-parse of the shared section-level extraction
    will not undo it.
    """
    override = db.scalar(
        select(UserOverride).where(
            UserOverride.user_id == user.id, UserOverride.assessment_id == assessment.id
        )
    )
    if override is None:
        override = UserOverride(user_id=user.id, assessment_id=assessment.id)
        db.add(override)
    override.dismissed = True

    if mapping is not None:
        db.delete(mapping)
