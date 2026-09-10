-- 14.1's grain, stated as a test. If this ever returns rows, something has
-- collapsed two instructors into one profile, which is precisely the averaging
-- the PRD forbids.

select
    course_id,
    instructor_id,
    term_id,
    count(*) as profile_count
from {{ ref('course_profiles') }}
group by course_id, instructor_id, term_id
having count(*) > 1
