-- One row per extracted graded item, typed and classified.
--
-- The assessment-family classification lives here rather than in the mart so
-- that "what counts as exam-heavy" is defined once. The families match
-- `services/pipeline/aggregate.py`, and the dbt test `assert_mix_families_match`
-- fails if the two ever drift apart.

with source as (

    select * from {{ source('app', 'assessments') }}

),

classified as (

    select
        id                as assessment_id,
        document_id,
        section_id,
        title,
        type              as assessment_type,
        category_ref,
        weight_pct,
        due_at,
        due_precision,
        time_inferred,
        is_group,
        effort_hours,
        confidence,
        verified_at,

        -- 14.3's assessment-mix facet.
        case
            when type in ('midterm', 'final_exam', 'quiz')                             then 'exam_heavy'
            when type in ('paper_short', 'paper_long', 'reading_response')             then 'paper_heavy'
            when type in ('final_project', 'project_milestone', 'presentation',
                          'lab_report')                                                then 'project_heavy'
            when type in ('problem_set', 'discussion_post', 'participation')           then 'continuous'
            else 'other'
        end as mix_family,

        type in ('midterm', 'final_exam') as is_exam,
        verified_at is not null           as is_verified

    from source

)

select * from classified
