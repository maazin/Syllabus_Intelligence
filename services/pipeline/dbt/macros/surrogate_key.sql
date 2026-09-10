{#
  A deterministic id from the columns that define a row's grain.

  Written locally rather than pulling in dbt_utils for one macro: the package
  would add a dependency, a lockfile, and a `dbt deps` step to CI for a hash.

  Determinism is the point. `course_profiles` is rebuilt nightly, and a random
  id would mean every row looks new to anything downstream that tracks them.
#}
{% macro surrogate_key(columns) -%}
    md5(
        {%- for column in columns %}
        coalesce(cast({{ column }} as varchar), '_null_')
        {%- if not loop.last %} || '|' || {% endif %}
        {%- endfor %}
    )
{%- endmacro %}
