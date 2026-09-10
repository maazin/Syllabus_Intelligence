-- The Layer 2 corpus (PRD section 14).
--
-- Grain: one row per course + instructor + term. Never averaged across
-- instructors, because 14.1 is emphatic that "a course taught by two people can
-- be two entirely different courses, and surfacing that difference is the whole
-- value proposition." Averaging would destroy the only signal students want.
--
-- Only publishable sections reach this table. The ones that do not are kept in
-- `corpus_coverage` with the reason, so the gap is visible rather than silent.

{{
    config(
        materialized = 'table',
        indexes = [
            {'columns': ['course_id']},
            {'columns': ['instructor_id']},
            {'columns': ['course_id', 'instructor_id', 'term_id'], 'unique': True},
        ]
    )
}}

with publishable as (

    select * from {{ ref('int_publishable_sections') }}
    where has_human_review and weights_validate

),

sections as (

    select * from {{ ref('stg_sections') }}

),

assessments as (

    select * from {{ ref('stg_assessments') }}

),

policies as (

    select * from {{ source('app', 'section_policies') }}

),

-- Weight held by each assessment family, which drives the mix label in 14.3.
family_weights as (

    select
        publishable.section_id,
        sum(assessments.weight_pct) filter (where assessments.mix_family = 'exam_heavy')    as exam_weight,
        sum(assessments.weight_pct) filter (where assessments.mix_family = 'paper_heavy')   as paper_weight,
        sum(assessments.weight_pct) filter (where assessments.mix_family = 'project_heavy') as project_weight,
        sum(assessments.weight_pct) filter (where assessments.mix_family = 'continuous')    as continuous_weight,
        sum(assessments.weight_pct)                                                          as total_weight,
        count(*)                                                                             as graded_item_count,
        count(*) filter (where assessments.is_exam)                                          as exam_count,
        bool_or(assessments.is_group)                                                        as has_group_assessment,
        sum(assessments.effort_hours)                                                        as total_effort_hours,
        count(*) filter (where assessments.is_verified)                                      as verification_count

    from publishable
    inner join assessments on assessments.document_id = publishable.document_id
    group by publishable.section_id

),

scored as (

    select
        publishable.course_id,
        publishable.instructor_id,
        publishable.term_id,
        publishable.document_id,
        sections.credits,
        sections.term_weeks,
        fw.*,

        -- Share of graded weight held by the largest family.
        case when coalesce(fw.total_weight, 0) = 0 then 0
             else greatest(
                 coalesce(fw.exam_weight, 0),
                 coalesce(fw.paper_weight, 0),
                 coalesce(fw.project_weight, 0),
                 coalesce(fw.continuous_weight, 0)
             ) / fw.total_weight
        end as top_family_share

    from publishable
    inner join sections on sections.section_id = publishable.section_id
    inner join family_weights as fw on fw.section_id = publishable.section_id

)

select
    {{ surrogate_key(["course_id", "instructor_id", "term_id"]) }} as id,
    course_id,
    instructor_id,
    term_id,
    document_id,

    -- 14.3's assessment-mix facet. Below the dominance threshold nothing leads,
    -- and "continuous" is the honest label: many small things rather than a few
    -- big ones, which is exactly the signal a student searching for a light
    -- elective is trying to find.
    case
        when total_weight is null or total_weight = 0 then 'unknown'
        when top_family_share < {{ var('mix_dominance_threshold') }} then 'continuous'
        when coalesce(exam_weight, 0)       >= greatest(coalesce(paper_weight, 0), coalesce(project_weight, 0), coalesce(continuous_weight, 0)) then 'exam_heavy'
        when coalesce(paper_weight, 0)      >= greatest(coalesce(exam_weight, 0),  coalesce(project_weight, 0), coalesce(continuous_weight, 0)) then 'paper_heavy'
        when coalesce(project_weight, 0)    >= greatest(coalesce(exam_weight, 0),  coalesce(paper_weight, 0),   coalesce(continuous_weight, 0)) then 'project_heavy'
        else 'continuous'
    end as assessment_mix_label,

    round(coalesce(exam_weight, 0)::numeric, 1)       as exam_weight_pct,
    round(coalesce(paper_weight, 0)::numeric, 1)      as paper_weight_pct,
    round(coalesce(project_weight, 0)::numeric, 1)    as project_weight_pct,
    round(coalesce(continuous_weight, 0)::numeric, 1) as continuous_weight_pct,

    -- 11.3: assessment effort spread across the term, plus the baseline floor.
    -- Computed the same way the personal heatmap computes it, because a student
    -- who filters for a six-hour course and then sees nine on their own
    -- timeline has caught the product contradicting itself.
    round(
        (
            coalesce(total_effort_hours, 0) / term_weeks
            + credits * {{ var('baseline_hours_per_credit') }}
        )::numeric,
        1
    ) as est_weekly_hours,

    graded_item_count,
    exam_count,
    has_group_assessment,
    verification_count,
    current_timestamp as computed_at

from scored
