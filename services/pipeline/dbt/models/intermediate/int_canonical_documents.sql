-- The one document per section that builds the public profile (section 12).
--
--   "Pick the most complete parse, breaking ties by verification count."
--
-- Several students upload variants of the same syllabus, an early draft, a
-- revised version, a photo of a printout. The corpus should reflect the best of
-- them rather than whichever happened to arrive first.

with documents as (

    select * from {{ source('app', 'syllabus_documents') }}
    -- 15.1: an instructor opt-out withdraws the document from the public layer
    -- immediately. It stays available to its uploader's own timeline.
    where visibility <> 'opted_out'

),

assessment_counts as (

    select
        document_id,
        count(*)                                   as assessment_count,
        count(*) filter (where is_verified)        as verified_count
    from {{ ref('stg_assessments') }}
    group by document_id

),

ranked as (

    select
        documents.id          as document_id,
        documents.section_id,
        documents.is_scanned,
        counts.assessment_count,
        counts.verified_count,

        row_number() over (
            partition by documents.section_id
            order by
                counts.assessment_count desc,
                counts.verified_count desc,
                -- A stable tiebreak, so a re-run does not reshuffle which
                -- document is canonical when two are genuinely equivalent.
                documents.id
        ) as rank_in_section

    from documents
    inner join assessment_counts as counts
        on counts.document_id = documents.id
    where counts.assessment_count > 0

)

select
    document_id,
    section_id,
    is_scanned,
    assessment_count,
    verified_count
from ranked
where rank_in_section = 1
