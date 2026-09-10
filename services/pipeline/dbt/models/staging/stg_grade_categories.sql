-- The grading breakdown per document, with the weight total that 14.2 gates on.

select
    document_id,
    section_id,
    count(*)          as category_count,
    sum(weight_pct)   as weight_total,
    sum(case when drop_lowest > 0 then 1 else 0 end) as categories_with_drops

from {{ source('app', 'grade_categories') }}
group by document_id, section_id
