-- A profile claiming zero or absurd weekly hours is a bug in the workload
-- model, not a real course. The floor alone is credits x 2 (11.3), so anything
-- at or below zero means the baseline was lost; anything past 60 hours a week
-- for one course means the effort constants have been misapplied.
--
-- This is a singular test rather than a column test because the plausible
-- range depends on the course's own credit count.

select
    id,
    course_id,
    est_weekly_hours
from {{ ref('course_profiles') }}
where est_weekly_hours <= 0
   or est_weekly_hours > 60
