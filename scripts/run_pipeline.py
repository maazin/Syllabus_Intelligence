#!/usr/bin/env python3
"""Run one syllabus through the whole pipeline and print what the student would see.

    python scripts/run_pipeline.py tests/fixtures/syllabi/cs_lecture_native_pdf.pdf
    python scripts/run_pipeline.py <file> --live      # calls the real model

Without `--live` this uses a recorded extraction for the bundled fixtures, so
the entire chain — ingestion, table extraction, date resolution, validation,
confidence scoring, heatmap, collision flags — runs with no API key and no
network. That is deliberate: everything except the model call is deterministic
and should be verifiable offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "services" / "worker" / "tasks"))

from date_resolver.calendar_context import (  # noqa: E402
    AcademicCalendar,
    CalendarExceptionEntry,
    FinalExamMatrixEntry,
)
from pipeline import PipelineResult, run_pipeline  # noqa: E402
from schemas.extraction import ExtractionOutput  # noqa: E402

RECORDED = ROOT / "tests" / "fixtures" / "recorded_extractions"


def load_calendar() -> AcademicCalendar:
    """Fall 2026, from `tests/fixtures/academic_calendar.json` (section 26)."""
    payload = json.loads((ROOT / "tests" / "fixtures" / "academic_calendar.json").read_text())
    matrix = json.loads((ROOT / "tests" / "fixtures" / "final_exam_matrix.json").read_text())
    tz = ZoneInfo(payload["timezone"])

    return AcademicCalendar(
        term_name=payload["term_name"],
        start_date=date.fromisoformat(payload["start_date"]),
        end_date=date.fromisoformat(payload["end_date"]),
        timezone=payload["timezone"],
        default_due_time=time.fromisoformat(payload["default_due_time"]),
        exceptions=tuple(
            CalendarExceptionEntry(
                date=date.fromisoformat(e["date"]), label=e["label"], no_class=e["no_class"]
            )
            for e in payload["exceptions"]
        ),
        add_drop_date=date.fromisoformat(payload["add_drop_date"]),
        withdrawal_date=date.fromisoformat(payload["withdrawal_date"]),
        finals_start=date.fromisoformat(payload["finals_start"]),
        finals_end=date.fromisoformat(payload["finals_end"]),
        final_exam_matrix=tuple(
            FinalExamMatrixEntry(
                meeting_pattern=row["meeting_pattern"],
                exam_start_at=datetime.fromisoformat(row["exam_start_at"]).replace(tzinfo=tz),
                exam_end_at=datetime.fromisoformat(row["exam_end_at"]).replace(tzinfo=tz),
            )
            for row in matrix["rows"]
        ),
    )


def recorded_extractor(fixture_name: str):
    """Return an extractor that replays a recorded extraction for this fixture."""

    def extract(document) -> ExtractionOutput:
        path = RECORDED / f"{fixture_name}.json"
        if not path.is_file():
            raise SystemExit(
                f"No recorded extraction for {fixture_name!r}.\n"
                f"Expected {path}.\n"
                "Re-run with --live to call the model, or record one for this fixture."
            )
        return ExtractionOutput.model_validate_json(path.read_text())

    return extract


def live_extractor(document):
    from extract import extract_document

    result = extract_document(document)
    print(
        f"  model={result.model} escalated={result.escalated} "
        f"cost={result.cost_cents:.4f}c latency={result.latency_ms}ms"
    )
    return result.output


def render(result: PipelineResult, calendar: AcademicCalendar) -> None:
    doc = result.document
    print("\n" + "=" * 78)
    print("INGESTION")
    print("=" * 78)
    print(f"  format          {doc.format.value}")
    print(f"  pages           {doc.page_count}")
    print(f"  scanned         {doc.is_scanned}")
    print(f"  est. tokens     {doc.estimated_tokens:,}")
    tables = [t for p in doc.pages for t in p.tables_markdown]
    print(f"  tables kept     {len(tables)}")

    course = result.extraction.course
    print("\n" + "=" * 78)
    print("EXTRACTION")
    print("=" * 78)
    print(f"  course          {course.subject_code} {course.catalog_number} — {course.title}")
    print(f"  instructor      {course.instructor.name}")
    print(f"  meeting         {course.meeting_pattern_raw}")

    total_weight = sum(c.weight_pct for c in result.extraction.grade_breakdown)
    print(f"\n  grade breakdown (sums to {total_weight:g}%):")
    for category in result.extraction.grade_breakdown:
        drop = f", drop {category.drop_lowest}" if category.drop_lowest else ""
        count = f" x{category.item_count}" if category.item_count else ""
        print(f"    {category.weight_pct:>5g}%  {category.category}{count}{drop}")

    policies = result.extraction.policies
    print(f"\n  attendance graded  {policies.attendance_graded}")
    print(f"  late policy        {policies.late_policy_class}")
    print(f"  final exam         {policies.final_exam_type}")
    print(f"  AI policy          {policies.ai_policy_class}")
    print(f"  group work         {policies.group_work_present}")

    print("\n" + "=" * 78)
    print("REVIEW QUEUE  (10.2: lowest confidence first)")
    print("=" * 78)
    print(f"  {'conf':>5}  {'bucket':<7} {'precision':<15} {'due':<12} title")
    print("  " + "-" * 74)
    for item in result.review_order:
        r = item.resolution
        if r.due_precision == "week_only":
            due = f"wk {r.week_start.strftime('%b %d')}"
        elif r.due_at:
            due = r.due_at.strftime("%Y-%m-%d")
            if r.time_inferred:
                due += "*"
        else:
            due = "—"
        marker = " !" if item.needs_explicit_action else "  "
        print(
            f"{marker}{item.confidence:>5.2f}  {item.bucket:<7} "
            f"{r.due_precision:<15} {due:<12} {item.title}"
        )
    print("\n  * time inferred, not stated in the syllabus (9.3) — shown muted in the UI")
    print(
        f"  auto-accepted (high confidence): {len(result.auto_accepted)}"
        f" of {len(result.review_items)}"
    )

    if result.unscheduled:
        print(f"\n  unscheduled tray ({len(result.unscheduled)} item(s), never on the calendar):")
        for item in result.unscheduled:
            print(f"    - {item.title}: {item.resolution.notes}")

    print("\n" + "=" * 78)
    print("VALIDATION  (9.4)")
    print("=" * 78)
    if not result.flags:
        print("  no flags — extraction passed every rule")
    for flag in result.flags:
        print(f"  [{flag.severity:<12}] {flag.check}")
        print(f"                 {flag.message}")
    print(f"\n  hallucination rate: {result.hallucination_rate:.1%} (section 16 ceiling: 0.5%)")

    print("\n" + "=" * 78)
    print("HEATMAP  (estimate — section 11.5)")
    print("=" * 78)
    peak = max((c.effort_hours for c in result.heatmap), default=1) or 1
    for cell in result.heatmap:
        week_no = calendar.week_number_for(cell.week_start)
        bar = "█" * int(round(cell.effort_hours / peak * 34))
        severity = ""
        if cell.week_start in result.collision_flags:
            top = result.collision_flags[cell.week_start][0]
            severity = "  <<" if top.severity == "red" else "  <"
        print(
            f"  wk {week_no:>2}  {cell.week_start.strftime('%b %d')}  "
            f"{cell.effort_hours:>5.1f}h  {bar}{severity}"
        )

    if result.collision_flags:
        print("\n" + "=" * 78)
        print("COLLISION FLAGS  (11.4)")
        print("=" * 78)
        for week in sorted(result.collision_flags):
            for flag in result.collision_flags[week]:
                print(f"  [{flag.severity:<6}] {flag.explanation}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument(
        "--live", action="store_true", help="call the real model instead of replaying a recording"
    )
    parser.add_argument("--meeting-pattern", default=None)
    parser.add_argument("--credits", type=float, default=3.0)
    parser.add_argument("--total-credits", type=float, default=15.0)
    args = parser.parse_args()

    if not args.file.is_file():
        raise SystemExit(f"No such file: {args.file}")

    calendar = load_calendar()
    extractor = live_extractor if args.live else recorded_extractor(args.file.stem)

    result = run_pipeline(
        args.file.name,
        args.file.read_bytes(),
        calendar,
        extractor=extractor,
        meeting_pattern=args.meeting_pattern,
        course_label=args.file.stem,
        credits=args.credits,
        total_enrolled_credits=args.total_credits,
        allow_ocr=args.live,
    )
    render(result, calendar)


if __name__ == "__main__":
    main()
