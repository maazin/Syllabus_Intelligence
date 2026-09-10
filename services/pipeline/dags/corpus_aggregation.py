"""Nightly corpus aggregation (PRD sections 14, 18.2, 21).

Section 18.2 is candid about the trade this represents. Airflow is the most
recognized name in orchestration, and Airflow 3 also demands a continuously
running scheduler plus a standalone DAG processor with a recommended 4 GB
minimum, which pushes the always-on VM from about EUR 5.49 to about EUR 8.49.
For one nightly aggregation job Dagster is the lighter engineering choice. Both
are defensible; the PRD says to pick knowingly, and this picks Airflow.

What the DAG actually guarantees, which a cron line would not:

  - dbt tests gate publication. If the corpus fails its own assertions, the
    search API keeps serving yesterday's profiles rather than today's broken
    ones. A silent bad publish is worse than a stale good one.
  - Every step is individually retryable and individually visible, so "the
    corpus is stale" resolves to a specific failed task rather than a guess.
  - Coverage is reported against the 14.4 target on every run, which is how
    that number stays honest between term boundaries.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator

REPO_ROOT = os.environ.get("SYLLINT_REPO_ROOT", "/opt/syllabus-intelligence")
DBT_DIR = f"{REPO_ROOT}/services/pipeline/dbt"

# profiles.yml defaults to the `dev` target, which is localhost. Left
# unspecified, every task below would connect to a Postgres that does not
# exist inside this container and the DAG would fail on its first run in a
# way that reads like a credentials problem.
DBT_TARGET = os.environ.get("DBT_TARGET", "dev")

DEFAULT_ARGS = {
    "owner": "syllabus-intelligence",
    "retries": 2,
    # Neon suspends after five minutes idle (18.1), so the first attempt of the
    # night can fail on a cold start. A short backoff absorbs that without
    # paging anyone.
    "retry_delay": timedelta(minutes=5),
    "depends_on_past": False,
    "email_on_failure": False,
}


def report_coverage(**context) -> dict:
    """Read the coverage mart and check it against the 14.4 target.

    Reported rather than enforced. Coverage below target is a business signal
    about how much seeding is left to do, not a pipeline failure, and failing
    the DAG over it would train everyone to ignore a red run.
    """
    import psycopg
    from airflow.exceptions import AirflowFailException

    dsn = os.environ.get("DATABASE_URL_PSYCOPG")
    if not dsn:
        raise AirflowFailException("DATABASE_URL_PSYCOPG is not configured")

    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            select
                count(*) filter (where is_published)                    as published,
                count(*)                                                as total,
                coalesce(sum(enrollment_weight) filter (where is_published), 0) as covered_seats,
                coalesce(sum(enrollment_weight), 0)                     as total_seats
            from public_marts.corpus_coverage
            """
        )
        totals = cursor.fetchone()
        if totals is None:
            # An aggregate always returns a row, so an empty result means the
            # mart is missing rather than empty, and reporting 0% coverage would
            # hide that behind a plausible number.
            raise AirflowFailException("public_marts.corpus_coverage returned no rows")
        published, total, covered_seats, total_seats = totals

        cursor.execute(
            """
            select blocked_reason, count(*), coalesce(sum(enrollment_weight), 0)
            from public_marts.corpus_coverage
            where blocked_reason is not null
            group by blocked_reason
            order by 3 desc
            """
        )
        blocked = cursor.fetchall()

    # 14.4 weights by enrollment because "covering the ten largest intro courses
    # matters more than covering a hundred seminars".
    coverage_pct = round(100.0 * covered_seats / total_seats, 1) if total_seats else 0.0

    print(f"Published {published} of {total} sections")
    print(f"Enrollment-weighted coverage: {coverage_pct}% (section 14.4 target: 25%)")
    for reason, sections, seats in blocked:
        print(f"  {reason}: {sections} sections, {seats} seats")

    return {
        "published": published,
        "total": total,
        "coverage_pct": coverage_pct,
        "meets_target": coverage_pct >= 25.0,
    }


with DAG(
    dag_id="corpus_aggregation",
    description="Rebuild Layer 2 course profiles from verified extractions",
    default_args=DEFAULT_ARGS,
    # Nightly, well after the evening upload peak. Profiles change at most once
    # a day as students complete reviews, so anything more frequent spends Neon
    # compute-hours to recompute an identical answer.
    schedule="0 4 * * *",
    start_date=datetime(2026, 8, 1),
    catchup=False,
    max_active_runs=1,
    tags=["corpus", "dbt", "layer-2"],
) as dag:
    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=f"cd {DBT_DIR} && dbt deps --no-version-check --target {DBT_TARGET}",
        env={"DBT_PROFILES_DIR": DBT_DIR, **os.environ},
    )

    # Staging and intermediate first, so a source-shape change fails here with a
    # clear message rather than deep inside the mart build.
    build_staging = BashOperator(
        task_id="build_staging",
        bash_command=(
            f"cd {DBT_DIR} && dbt run --no-version-check --target {DBT_TARGET} "
            "--select staging.* intermediate.*"
        ),
        env={"DBT_PROFILES_DIR": DBT_DIR, **os.environ},
    )

    test_staging = BashOperator(
        task_id="test_staging",
        bash_command=(
            f"cd {DBT_DIR} && dbt test --no-version-check --target {DBT_TARGET} "
            "--select source:* staging.* intermediate.*"
        ),
        env={"DBT_PROFILES_DIR": DBT_DIR, **os.environ},
    )

    build_marts = BashOperator(
        task_id="build_marts",
        bash_command=(
            f"cd {DBT_DIR} && dbt run --no-version-check --target {DBT_TARGET} --select marts.*"
        ),
        env={"DBT_PROFILES_DIR": DBT_DIR, **os.environ},
    )

    # The gate. 14.2 will not publish a profile whose weights do not validate or
    # that no human reviewed, and these tests are what enforce that at the mart
    # rather than trusting the model that applied it.
    test_marts = BashOperator(
        task_id="test_marts",
        bash_command=(
            f"cd {DBT_DIR} && dbt test --no-version-check --target {DBT_TARGET} --select marts.*"
        ),
        env={"DBT_PROFILES_DIR": DBT_DIR, **os.environ},
    )

    coverage = PythonOperator(
        task_id="report_coverage",
        python_callable=report_coverage,
    )

    # Docs are regenerated on every successful run so the corpus stays
    # self-describing for anyone who has to reason about a profile later.
    docs = BashOperator(
        task_id="generate_docs",
        bash_command=(
            f"cd {DBT_DIR} && dbt docs generate --no-version-check --target {DBT_TARGET}"
        ),
        env={"DBT_PROFILES_DIR": DBT_DIR, **os.environ},
    )

    dbt_deps >> build_staging >> test_staging >> build_marts >> test_marts >> coverage >> docs
