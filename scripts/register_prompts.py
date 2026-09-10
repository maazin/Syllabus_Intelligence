#!/usr/bin/env python3
"""Push the prompt files into the MLflow registry (PRD sections 16, 18.1).

    python scripts/register_prompts.py
    python scripts/register_prompts.py --promote   # also move the production alias

Run on deploy. MLflow versions are immutable, so registering unchanged text
returns the existing version rather than creating a duplicate.

Promotion is a separate flag on purpose. Section 27.2 treats a worse prompt as
exactly as blocking as a failing test, so moving production onto a new version
should follow the eval gate rather than ride along with registration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "packages" / "core")]

from prompts import CURRENT_PROMPT_VERSION  # noqa: E402
from prompts.registry import (  # noqa: E402
    is_enabled,
    promote_to_production,
    register_prompts,
    tracking_uri,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=CURRENT_PROMPT_VERSION)
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Point the production alias at the versions just registered",
    )
    args = parser.parse_args()

    if not is_enabled():
        raise SystemExit(
            "MLFLOW_TRACKING_URI is not set. Start the tracking server with\n"
            "  docker compose -f infra/docker/docker-compose.yml up -d mlflow\n"
            "and export MLFLOW_TRACKING_URI=http://localhost:5100"
        )

    print(f"Registering prompt files version {args.version} to {tracking_uri()}")
    registered = register_prompts(args.version)

    for name, version in registered.items():
        print(f"  {name}: registry version {version}")
        if args.promote:
            promote_to_production(name, version)
            print("    promoted to production")

    if not args.promote:
        print("\nNot promoted. Run the eval gate first, then re-run with --promote.")


if __name__ == "__main__":
    main()
