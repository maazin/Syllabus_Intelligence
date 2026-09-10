-- Sections joined to the course, instructor, and term they belong to.
--
-- 14.1's aggregation unit is course + instructor + term, so this model is where
-- that grain is established. A section with no instructor cannot be profiled at
-- all: attributing a course's workload to nobody is worse than omitting it,
-- because instructor variance is the entire point of the feature.

with sections as (

    select * from {{ source('app', 'sections') }}

),

courses as (

    select * from {{ source('app', 'courses') }}

),

instructors as (

    select * from {{ source('app', 'instructors') }}

),

terms as (

    select * from {{ source('app', 'terms') }}

)

select
    sections.id            as section_id,
    sections.section_code,
    sections.meeting_pattern,
    sections.modality,
    sections.seats_total,
    sections.seats_open,

    courses.id             as course_id,
    courses.subject_code,
    courses.catalog_number,
    courses.title          as course_title,
    courses.credits,
    courses.gened_attributes,

    instructors.id         as instructor_id,
    instructors.name       as instructor_name,

    terms.id               as term_id,
    terms.name             as term_name,
    terms.start_date       as term_start_date,
    coalesce(terms.finals_end, terms.end_date) as term_last_day,

    -- The denominator for "hours per week". Finals week counts: 9.5 is explicit
    -- that a term ending at week 14 misses the densest stretch of the term.
    greatest(
        1,
        (coalesce(terms.finals_end, terms.end_date) - terms.start_date) / 7
    ) as term_weeks

from sections
inner join courses     on courses.id     = sections.course_id
inner join terms       on terms.id       = sections.term_id
inner join instructors on instructors.id = sections.instructor_id
