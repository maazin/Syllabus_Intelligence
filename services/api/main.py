"""FastAPI application — PRD section 23.

HTTP surface only. No parsing, no resolution, no workload math lives here
(section 21's rule): the API validates, persists, enqueues, and reads back.
Anything that thinks belongs in `packages/core` or `services/worker`.

The OpenAPI spec generated from this app at `/openapi.json` is the source of
truth for the contract once routes exist — section 23's table gives shape only.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.api.routers import (
    account,
    assessments,
    auth,
    calendar,
    courses,
    documents,
    plan,
    search,
    terms,
)

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Syllabus Intelligence API",
    version="0.1.0",
    description=(
        "Layer 1 (personal timeline) and Layer 2 (course corpus). "
        "See the PRD for the contract this implements."
    ),
    docs_url="/docs",
    openapi_url="/openapi.json",
)

# The Angular app is served from a different origin in dev (ng serve on :4200)
# and from Cloudflare Pages in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "http://localhost:4200").split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(account.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(assessments.router, prefix=API_PREFIX)
app.include_router(plan.router, prefix=API_PREFIX)
app.include_router(calendar.router, prefix=API_PREFIX)
app.include_router(courses.router, prefix=API_PREFIX)
app.include_router(search.router, prefix=API_PREFIX)
app.include_router(terms.router, prefix=API_PREFIX)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok", "environment": os.environ.get("ENVIRONMENT", "local")}
