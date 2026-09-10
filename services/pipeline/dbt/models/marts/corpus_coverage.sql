-- What the corpus is still missing, and why (PRD section 14.4).
--
-- The coverage target is enrollment-weighted: "covering the ten largest intro
-- courses matters more than covering a hundred seminars." This model keeps the
-- unpublished sections alongside the published ones with the specific reason
-- each is blocked, which turns coverage from a single percentage into a
-- worklist somebody can actually act on.

{{ config(materialized = 'table') }}

with sections as (

    select * from {{ ref('stg_sections') }}

),

publishable as (

    select * from {{ ref('int_publishable_sections') }}

)

select
    sections.section_id,
    sections.course_id,
    sections.subject_code,
    sections.catalog_number,
    sections.course_title,
    sections.instructor_id,
    sections.instructor_name,
    sections.term_id,
    sections.term_name,
    sections.seats_total,

    publishable.document_id is not null as has_document,
    coalesce(publishable.has_human_review, false) as has_human_review,
    coalesce(publishable.weights_validate, false) as weights_validate,

    coalesce(publishable.has_human_review and publishable.weights_validate, false)
        as is_published,

    -- Null means published. A blanket coalesce here would relabel every
    -- successful section as "no_document", which is how a coverage report
    -- starts claiming zero coverage while the profiles table is full.
    case
        when publishable.section_id is null then 'no_document'
        else publishable.blocked_reason
    end as blocked_reason,

    -- Enrollment weight, so the worklist sorts by how much coverage each
    -- section actually buys.
    coalesce(sections.seats_total, 0) as enrollment_weight

from sections
left join publishable on publishable.section_id = sections.section_id
