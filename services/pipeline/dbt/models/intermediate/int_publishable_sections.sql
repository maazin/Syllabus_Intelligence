-- Section 14.2's publication gate, expressed once.
--
-- A profile is published only when:
--   1. at least one document for the section has completed human review,
--   2. the grade weights validate within tolerance, and
--   3. the uploader has not withdrawn the document.
--
-- The third is handled upstream in `int_canonical_documents`. This model adds
-- the first two and, importantly, keeps the sections that fail along with the
-- reason they failed. Those rows are the corpus-coverage worklist from 14.4:
-- they say exactly what is missing before a section can be published, which is
-- more useful than silently filtering them away.

with canonical as (

    select * from {{ ref('int_canonical_documents') }}

),

sections as (

    select * from {{ ref('stg_sections') }}

),

categories as (

    select * from {{ ref('stg_grade_categories') }}

)

select
    sections.section_id,
    sections.course_id,
    sections.instructor_id,
    sections.term_id,
    canonical.document_id,
    canonical.assessment_count,
    canonical.verified_count,
    categories.weight_total,

    canonical.verified_count > 0 as has_human_review,

    coalesce(
        categories.weight_total between {{ var('weight_sum_min') }}
                                    and {{ var('weight_sum_max') }},
        false
    ) as weights_validate,

    case
        when categories.weight_total is null   then 'no_grade_breakdown'
        when canonical.verified_count = 0      then 'no_human_review'
        when not (categories.weight_total between {{ var('weight_sum_min') }}
                                              and {{ var('weight_sum_max') }})
                                               then 'weights_do_not_validate'
        else null
    end as blocked_reason

from canonical
inner join sections   on sections.section_id   = canonical.section_id
left join  categories on categories.document_id = canonical.document_id
