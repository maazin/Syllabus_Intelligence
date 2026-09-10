"""Pydantic models mirroring PRD section 8.2 exactly.

This is the extraction contract: the JSON shape both LLM passes (section 25)
must validate against, and the shape `services/worker/tasks/extract.py` writes
into `extraction_runs.raw_output` before it is normalized into DB rows.

Do not add fields here that the prompt cannot ground in a `source_span`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Modality = Literal["in_person", "hybrid", "online_sync", "online_async"]

AssessmentType = Literal[
    "midterm",
    "final_exam",
    "quiz",
    "problem_set",
    "paper_short",
    "paper_long",
    "lab_report",
    "project_milestone",
    "final_project",
    "presentation",
    "discussion_post",
    "reading_response",
    "participation",
    "other",
]

LatePolicyClass = Literal[
    "none_accepted",
    "flat_penalty",
    "per_day_penalty",
    "grace_tokens",
    "case_by_case",
    "unspecified",
]

FinalExamType = Literal["cumulative", "non_cumulative", "final_project", "none", "unspecified"]

AiPolicyClass = Literal["prohibited", "permitted_with_disclosure", "permitted", "unspecified"]


class InstructorInfo(BaseModel):
    name: str | None = None
    email: str | None = None
    office_hours_raw: str | None = None


class CourseMetadata(BaseModel):
    subject_code: str | None = None
    catalog_number: str | None = None
    section_code: str | None = None
    title: str | None = None
    credits: float | None = None
    instructor: InstructorInfo = Field(default_factory=InstructorInfo)
    term_hint: str | None = None
    modality: Modality | None = None
    meeting_pattern_raw: str | None = None


class GradeBreakdownItem(BaseModel):
    category: str
    weight_pct: float
    item_count: int | None = None
    drop_lowest: int = 0


class AssessmentExtraction(BaseModel):
    title: str
    type: AssessmentType
    category_ref: str | None = None
    weight_pct: float | None = None
    date_expression_raw: str | None = Field(
        default=None,
        description="Verbatim date expression from the source. Never a fabricated ISO date.",
    )
    recurrence_raw: str | None = None
    is_group: bool = False
    page_ref: int | None = None
    source_span: str = Field(
        ..., description="Verbatim quote from the source text. Unquotable means not extracted."
    )
    confidence: float = Field(..., ge=0.0, le=1.0)

    @field_validator("source_span")
    @classmethod
    def source_span_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source_span must be a non-empty verbatim quote")
        return v


class RequiredMaterial(BaseModel):
    title: str
    isbn: str | None = None
    required: bool = True
    est_cost_usd: float | None = None


class Policies(BaseModel):
    attendance_graded: bool | None = None
    attendance_weight_pct: float | None = None
    attendance_policy_raw: str | None = None
    late_policy_class: LatePolicyClass = "unspecified"
    late_policy_raw: str | None = None
    group_work_present: bool | None = None
    group_work_weight_pct: float | None = None
    final_exam_type: FinalExamType = "unspecified"
    curve_mentioned: bool | None = None
    participation_weight_pct: float | None = None
    ai_policy_class: AiPolicyClass = "unspecified"
    required_materials: list[RequiredMaterial] = Field(default_factory=list)


class PassAOutput(BaseModel):
    """Pass A (25.1): course metadata, grade breakdown, policies. No assessments."""

    course: CourseMetadata
    grade_breakdown: list[GradeBreakdownItem] = Field(default_factory=list)
    policies: Policies = Field(default_factory=Policies)


class PassBOutput(BaseModel):
    """Pass B (25.2): the assessment schedule, linked to Pass A's categories."""

    assessments: list[AssessmentExtraction] = Field(default_factory=list)


class ExtractionOutput(BaseModel):
    """The merged view of both passes — what section 8.2 shows as one document."""

    course: CourseMetadata
    grade_breakdown: list[GradeBreakdownItem] = Field(default_factory=list)
    assessments: list[AssessmentExtraction] = Field(default_factory=list)
    policies: Policies = Field(default_factory=Policies)

    @classmethod
    def from_passes(cls, pass_a: PassAOutput, pass_b: PassBOutput) -> ExtractionOutput:
        return cls(
            course=pass_a.course,
            grade_breakdown=pass_a.grade_breakdown,
            policies=pass_a.policies,
            assessments=pass_b.assessments,
        )
