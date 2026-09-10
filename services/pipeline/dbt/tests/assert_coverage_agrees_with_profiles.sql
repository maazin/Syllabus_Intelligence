-- The two marts must tell the same story.
--
-- `corpus_coverage` marking a section published while `course_profiles` has no
-- row for it (or the reverse) means one of them is lying, and a coverage report
-- that disagrees with the corpus it describes is worse than no report. This
-- caught a real bug: a blanket coalesce on `blocked_reason` relabelled every
-- published section as "no_document", so coverage read zero while the profiles
-- table was full.

with coverage_published as (

    select count(distinct section_id) as n
    from {{ ref('corpus_coverage') }}
    where is_published

),

profiles as (

    select count(*) as n from {{ ref('course_profiles') }}

)

select
    coverage_published.n as coverage_says,
    profiles.n           as profiles_has
from coverage_published
cross join profiles
where coverage_published.n <> profiles.n
