"""Layer 2 corpus aggregation — PRD section 14.

Turns verified extractions into per-course-per-instructor-per-term profiles.

The aggregation unit is **course + instructor + term**, and the PRD is emphatic
about why: "A course taught by two people can be two entirely different courses,
and surfacing that difference is the whole value proposition." Averaging across
instructors would destroy the only signal students actually want.

Publication is gated (14.2). A profile appears only when a human has reviewed
the document, the grade weights validate, and the uploader has not marked it
private. An unvalidated profile is worse than no profile: the search product's
entire credibility rests on the numbers being defensible.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from db.models import (
    Assessment,
    Course,
    CourseProfile,
    GradeCategory,
    Section,
    SectionPolicies,
    SyllabusDocument,
    Term,
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from workload_model.effort import EffortInput, effort_hours

logger = logging.getLogger(__name__)

#: 9.4's tolerance, reused as 14.2's publication gate.
WEIGHT_SUM_MIN = 98.0
WEIGHT_SUM_MAX = 102.0

#: Assessment-mix labels (14.3). A course is labeled by whichever family holds
#: the plurality of graded weight, unless nothing dominates — in which case the
#: honest label is "continuous", meaning many small things rather than a few big ones.
_MIX_FAMILIES: dict[str, set[str]] = {
    "exam_heavy": {"midterm", "final_exam", "quiz"},
    "paper_heavy": {"paper_short", "paper_long", "reading_response"},
    "project_heavy": {"final_project", "project_milestone", "presentation", "lab_report"},
    "continuous": {"problem_set", "discussion_post", "participation"},
}

MIX_DOMINANCE_THRESHOLD = 0.40


@dataclass
class AggregationReport:
    published: int = 0
    skipped: int = 0
    reasons: Counter = field(default_factory=Counter)


def assessment_mix(assessments: Sequence[Assessment]) -> dict:
    """Classify how a course distributes graded weight across assessment families."""
    totals: dict[str, float] = dict.fromkeys(_MIX_FAMILIES, 0.0)
    for item in assessments:
        weight = item.weight_pct or 0
        for family, types in _MIX_FAMILIES.items():
            if item.type in types:
                totals[family] += weight
                break

    grand_total = sum(totals.values())
    if grand_total <= 0:
        return {"label": "unknown", "weights": totals}

    shares = {family: value / grand_total for family, value in totals.items()}
    label, top_share = max(shares.items(), key=lambda kv: kv[1])
    if top_share < MIX_DOMINANCE_THRESHOLD:
        # Nothing dominates: this is a course of many small things, which is
        # exactly the "constant small deadlines" signal students search for.
        label = "continuous"
    return {"label": label, "weights": {k: round(v, 1) for k, v in totals.items()}}


def estimated_weekly_hours(
    assessments: Sequence[Assessment], credits: float, term_weeks: int
) -> float:
    """Total projected effort spread over the term, plus the baseline floor (11.3).

    This is the number the "estimated weekly hours" facet sorts on, so it must be
    computed the same way the personal heatmap computes it — a student who
    filtered for a 6-hour course and then sees 9 hours on their own timeline has
    caught the product contradicting itself.
    """
    if term_weeks <= 0:
        return 0.0

    assessment_hours = sum(
        effort_hours(
            EffortInput(
                assessment_type=a.type,
                credits=credits,
                weight_pct=a.weight_pct,
                is_group=a.is_group,
            )
        )
        for a in assessments
    )
    baseline = credits * 2.0  # 11.3, per week
    return round(assessment_hours / term_weeks + baseline, 1)


def _publishable(
    document: SyllabusDocument,
    assessments: Sequence[Assessment],
    categories: Sequence[GradeCategory],
) -> tuple[bool, str]:
    """Section 14.2's three conditions, in the order that fails cheapest."""
    if document.visibility != "private" and document.visibility != "public":
        # `opted_out` (15.1) or anything else non-standard withdraws the profile.
        return False, "instructor_opted_out"
    if not any(a.verified_at is not None for a in assessments):
        return False, "no_human_review"
    if not categories:
        return False, "no_grade_breakdown"

    total = sum(c.weight_pct for c in categories)
    if not (WEIGHT_SUM_MIN <= total <= WEIGHT_SUM_MAX):
        return False, "weights_do_not_validate"
    return True, ""


def aggregate_term(db: Session, term_id, *, dry_run: bool = False) -> AggregationReport:
    """Recompute course profiles for one term.

    Runs as a batch job (18.2's nightly orchestration), not in a request path:
    it touches every section in the term and the output changes at most once a
    day as students complete reviews.
    """
    report = AggregationReport()
    term = db.scalar(select(Term).where(Term.id == term_id))
    if term is None:
        raise ValueError(f"No term {term_id}")

    term_weeks = max(1, ((term.finals_end or term.end_date) - term.start_date).days // 7)

    sections = db.scalars(select(Section).where(Section.term_id == term_id)).all()
    for section in sections:
        if section.instructor_id is None:
            report.skipped += 1
            report.reasons["no_instructor"] += 1
            continue

        documents = db.scalars(
            select(SyllabusDocument).where(SyllabusDocument.section_id == section.id)
        ).all()
        if not documents:
            report.skipped += 1
            report.reasons["no_document"] += 1
            continue

        canonical = _pick_canonical(db, documents)
        if canonical is None:
            report.skipped += 1
            report.reasons["no_canonical_document"] += 1
            continue

        assessments = db.scalars(
            select(Assessment).where(Assessment.document_id == canonical.id)
        ).all()
        categories = db.scalars(
            select(GradeCategory).where(GradeCategory.document_id == canonical.id)
        ).all()

        ok, reason = _publishable(canonical, assessments, categories)
        if not ok:
            report.skipped += 1
            report.reasons[reason] += 1
            continue

        if not dry_run:
            _upsert_profile(db, section, canonical, assessments, term_weeks)
        report.published += 1

    if not dry_run:
        db.commit()

    logger.info(
        "Aggregated %s: %d profile(s) published, %d skipped (%s)",
        term.name,
        report.published,
        report.skipped,
        dict(report.reasons),
    )
    return report


def _pick_canonical(db: Session, documents: Sequence[SyllabusDocument]) -> SyllabusDocument | None:
    """Choose the one document that builds the public profile (section 12).

    "Pick the most complete parse, breaking ties by verification count." Several
    students upload variants of the same syllabus — an early draft, a revised
    version, a photo of a printout — and the corpus should reflect the best of
    them rather than whichever arrived first.
    """
    best: tuple[int, int, SyllabusDocument] | None = None
    for document in documents:
        if document.visibility == "opted_out":
            continue
        assessments = db.scalars(
            select(Assessment).where(Assessment.document_id == document.id)
        ).all()
        if not assessments:
            continue
        verified = sum(1 for a in assessments if a.verified_at is not None)
        score = (len(assessments), verified, document)
        if best is None or score[:2] > best[:2]:
            best = score

    if best is None:
        return None

    chosen = best[2]
    for document in documents:
        document.canonical_for_section = document.id == chosen.id
    return chosen


def _upsert_profile(
    db: Session,
    section: Section,
    document: SyllabusDocument,
    assessments: Sequence[Assessment],
    term_weeks: int,
) -> CourseProfile:
    course = db.scalar(select(Course).where(Course.id == section.course_id))
    policies = db.scalar(select(SectionPolicies).where(SectionPolicies.section_id == section.id))

    profile = db.scalar(
        select(CourseProfile).where(
            CourseProfile.course_id == section.course_id,
            CourseProfile.instructor_id == section.instructor_id,
            CourseProfile.term_id == section.term_id,
        )
    )
    if profile is None:
        profile = CourseProfile(
            course_id=section.course_id,
            instructor_id=section.instructor_id,
            term_id=section.term_id,
        )
        db.add(profile)

    profile.computed_at = datetime.now(UTC)
    profile.assessment_mix = assessment_mix(assessments)
    profile.est_weekly_hours = estimated_weekly_hours(
        assessments, course.credits if course else 3.0, term_weeks
    )
    profile.has_group_project = any(a.is_group for a in assessments) or bool(
        policies and policies.group_work_present
    )
    profile.attendance_graded = bool(policies and policies.attendance_graded)
    profile.exam_count = sum(1 for a in assessments if a.type in ("midterm", "final_exam"))
    profile.final_exam_type = policies.final_exam_type if policies else "unspecified"
    profile.graded_item_count = len(assessments)
    profile.est_materials_cost_usd = policies.est_materials_cost_usd if policies else None
    profile.source_document_ids = [document.id]
    profile.verification_count = sum(1 for a in assessments if a.verified_at is not None)
    return profile
