#!/usr/bin/env python3
"""Recompute Layer 2 course profiles — PRD section 14.

    python scripts/aggregate_corpus.py                 # every term
    python scripts/aggregate_corpus.py --term "Fall 2026"
    python scripts/aggregate_corpus.py --dry-run       # report without writing

This is the nightly batch job (18.2). It is deliberately a script rather than an
endpoint: it touches every section in a term, and its output changes at most
once a day as students complete reviews.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "packages" / "core")]

from db.models import Term  # noqa: E402
from db.session import SessionLocal  # noqa: E402
from sqlalchemy import select  # noqa: E402

from services.pipeline.aggregate import aggregate_term  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--term", default=None, help="Term name, e.g. 'Fall 2026'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        query = select(Term)
        if args.term:
            query = query.where(Term.name == args.term)
        terms = db.scalars(query).all()
        if not terms:
            raise SystemExit(f"No term matching {args.term!r}")

        for term in terms:
            report = aggregate_term(db, term.id, dry_run=args.dry_run)
            print(f"\n{term.name}{'  (dry run)' if args.dry_run else ''}")
            print(f"  published  {report.published}")
            print(f"  skipped    {report.skipped}")
            for reason, count in report.reasons.most_common():
                # Skip reasons are the corpus-coverage worklist (14.4): they say
                # exactly what is missing before a section can be published.
                print(f"    {count:>4}  {reason}")


if __name__ == "__main__":
    main()
