-- Section 14.2's first condition, asserted at the mart rather than trusted from
-- the model that applies it. An unreviewed extraction reaching the public
-- corpus is the failure that would cost the product its credibility, so it is
-- worth checking twice.

select
    id,
    course_id,
    verification_count
from {{ ref('course_profiles') }}
where verification_count < 1
