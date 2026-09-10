"""Root pytest configuration.

Puts the monorepo's packages on `sys.path` so `pytest` works from a clean
checkout with no PYTHONPATH incantation. `packages/core` is imported by
`services/*` as a library (section 21's rule: import it, never copy it), and
this is what makes that import resolve in tests the same way it does in the
Docker images.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for path in (
    ROOT,  # `services.api...`, `services.worker...`
    ROOT / "packages" / "core",  # `schemas`, `db`, `date_resolver`, ...
    ROOT / "services" / "worker" / "tasks",  # `ingest`, `extract`, `validate`, ...
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
