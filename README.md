# Syllabus Intelligence

[![CI](https://github.com/maazin/Syllabus_Intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/maazin/Syllabus_Intelligence/actions/workflows/ci.yml)

Students upload their syllabi in week 1. The system extracts every graded item,
resolves relative dates against the institution's academic calendar, merges
everything into one timeline, and renders a workload heatmap that flags
collision weeks before they arrive.

Built against `syllabus-intelligence-prd.md`. Section references throughout the
code point back to that document — when the code and the PRD disagree, that is
a bug in one of them worth resolving explicitly.

---

## What exists today

| Area | State |
|---|---|
| Repo scaffold, monorepo layout (§21) | done |
| Data model, migrations, seed (§12, §26) | done — 19 tables, idempotent seed |
| Ingestion: PDF/DOCX/HTML/text, table extraction, scan detection (§7) | done |
| OCR routing to Docling (§18.1) | wired; needs the `docling` extra installed |
| Two-pass extraction, versioned prompts, model cascade (§8, §25) | done; needs an API key to run |
| Date resolver — every row of §9.2, plus the exam matrix (§9.5) | done, 40 unit tests |
| Validation rules (§9.4), confidence scoring (§10.1) | done |
| Workload model, heatmap, collision flags (§11) | done |
| API surface (§23) | done — all routes, auth, uploads, timeline, heatmap, ICS |
| Celery worker: upload → parse → persisted rows | done, verified through a live worker |
| ICS calendar feed (§13) | done |
| Google Calendar sync (§13) | done — OAuth flow, event diffing, deletion handling; unrun against real Google |
| Account deletion (§15.2) | done |
| Layer 2 corpus + faceted search (§14) | done — §14.3's named query passes end to end |
| Eval harness + CI gate (§16, §27.2) | harness done; golden set empty by design |
| Angular web app (§18.1, §21) | done — sign-in, upload, review, timeline, heatmap, search |
| dbt models for the corpus (§21) | done — 7 models, 24 tests, runs against Postgres |
| Airflow orchestration (§18.2) | done — nightly DAG, runs green end to end |
| MLflow prompt registry, tracing, evals (§16, §18.1) | done — prompts registered and promoted |
| Terraform for Cloud Run, Neon, R2, Cloudflare (§18.1) | done — validates; needs your accounts to apply |

**234 Python tests and 122 browser tests pass.** Run `pytest`, and `npm --prefix apps/web run e2e`.

---

## Quick start

```bash
cp .env.example .env
docker compose -f infra/docker/docker-compose.yml up -d postgres redis minio mailhog
```

```bash
uv venv && uv pip install -r services/api/requirements.txt pdfplumber python-docx beautifulsoup4 reportlab pillow pytest anthropic
```

Apply migrations and seed the academic calendar, exam matrix, and catalog:

```bash
cd packages/core && alembic upgrade head && cd ../..
```

```bash
python scripts/generate_fixture_syllabi.py && python scripts/seed_dev_data.py
```

Run the API:

```bash
uvicorn services.api.main:app --reload --port 8100
```

Run the web app (it proxies `/api` to the API above):

```bash
npm --prefix apps/web start
```

Local services (host ports are offset so this stack coexists with other projects):

| Service | URL |
|---|---|
| API + OpenAPI docs | http://localhost:8100/docs |
| MailHog (catches magic links) | http://localhost:8125 |
| MinIO console | http://localhost:9101 |
| Web app | http://localhost:4200 |
| MLflow | http://localhost:5100 |
| Airflow | http://localhost:8180 (airflow / airflow) |
| Postgres | `localhost:5533` |

---

## See the pipeline run

The fastest way to understand what this does:

```bash
python scripts/run_pipeline.py tests/fixtures/syllabi/cs_lecture_native_pdf.pdf --meeting-pattern "MWF 10:00-10:50"
```

That runs a real PDF through ingestion, table extraction, date resolution,
validation, confidence scoring, the workload model, and collision flags, then
prints the review queue and heatmap. It uses a recorded extraction, so it needs
no API key and no network — everything except the model call is deterministic
and verifiable offline. Add `--live` to call the real model.

### The full loop

With the stack up, an upload goes all the way to reviewable rows without any
manual step:

```bash
curl -X POST "http://localhost:8100/api/v1/documents?section_id=<id>" -H "Authorization: Bearer <token>" -F "files=@syllabus.pdf"
```

The API stores the file, dedupes it by content hash, and enqueues a parse. The
worker fetches it, ingests, extracts, resolves dates against the seeded
registrar calendar, validates, scores confidence, precomputes effort, and writes
assessments the `/timeline` and `/heatmap` endpoints serve back. A re-upload of
identical bytes returns the existing parse instantly and never reaches the queue.

On the bundled fixture it demonstrates the behaviors the PRD cares most about:

- **"Tuesday, October 14"** is flagged as a weekday/date mismatch (Oct 14 2026 is
  a Wednesday), resolved to the numeric date, and pushed to the bottom of the
  review queue at 0.43 confidence so it cannot auto-accept.
- **The final exam** resolves from the registrar matrix to Dec 9, not from the
  syllabus text, which only says "see the university final exam schedule."
- **Every date without a stated time** renders as `date_only` with an inference
  marker. No fabricated times reach a calendar.
- **Hallucination rate is 0.0%** — every extracted item quotes the source verbatim.

### One thing worth knowing about time

`due_at` is stored as `timestamptz`, so Postgres hands it back in UTC. Every
display surface works in *calendar days*, and 11:59pm local — the LMS default
due time (§9.3), which is to say most deadlines — is the next day in UTC.
Anything deriving a day from a stored `due_at` must go through
`db.localtime.local_date` first. `packages/core/tests/test_localtime.py` pins
this down; getting it wrong moves nearly every deadline in the product by one
day, silently.

---

## The data and infrastructure stack

Four tools the PRD specifies were left as empty folders. They are built now, and
each one runs locally rather than existing only as config.

**dbt** (`services/pipeline/dbt`) builds the Layer 2 corpus. §18.1 calls dbt
"unambiguously the right tool for `course_profiles`", and it is: the aggregation
is relational transformation over rows that already exist, so writing it as SQL
makes it inspectable and lets the publication rules from §14.2 become assertions
rather than comments. Seven models, twenty-four tests.

```bash
cd services/pipeline/dbt && dbt build
```

The `corpus_coverage` mart is the piece worth pointing at. It keeps the sections
that failed to publish alongside the ones that succeeded, with the reason each
is blocked, which turns §14.4's coverage target from a single percentage into a
worklist. It also computes the enrollment-weighted number §14.4 actually names.

**Airflow** (`services/pipeline/dags`) orchestrates the nightly rebuild. dbt
tests gate publication, so a corpus that fails its own assertions leaves
yesterday's profiles serving rather than replacing them with today's broken
ones. Airflow runs in its own container: Airflow 2.x caps SQLAlchemy below 2.0
while the app needs 2.x, and installing them together silently breaks the API.
That conflict is the concrete form of the trade §18.2 describes.

**MLflow** is the prompt registry, the trace store, and the eval tracker, which
is why §18.1 picks it over three separate tools. Every extraction run is tagged
with the prompt version that produced it, which is what makes §16's claim
operational: a production correction can be attributed to a specific prompt.
Promotion is a separate step from registration, because §27.2 treats a worse
prompt as exactly as blocking as a failing test.

```bash
python scripts/register_prompts.py --promote
```

**Terraform** (`infra/terraform`) describes Cloud Run, the GKE cluster, Neon,
R2, and Cloudflare. `terraform validate` passes; applying it needs your cloud
accounts. Writing it surfaced two errors that would have failed at apply time:
the v4 Cloudflare provider has no R2 lifecycle resource, and the Neon attribute
is `default_branch_id` rather than `branch_id`. The lifecycle gap is documented
in `storage.tf` with the wrangler command that covers it, rather than quietly
dropped.

**Kubernetes** (`infra/k8s`) runs the four workloads that cannot be serverless:
Redis, the Celery workers, MLflow, and Airflow. They were a Docker Compose file
on a VM, deployed by restarting the instance. Moving them onto GKE bought three
things worth the move: rolling updates with a health gate instead of a hard
restart, KEDA scaling the workers on Redis queue depth rather than CPU (a
worker blocked on the model API is idle, so CPU scales the pool down exactly
when the backlog is deepest), and Airflow on its own Helm chart instead of
`airflow standalone`, which was a development mode and a listed gap.

Rendering the chart before deploying it caught the thing most likely to have
cost an afternoon: the unpinned chart deploys the Airflow 3 topology, which
produces perfectly valid manifests describing components that the 2.10.5 image
has no commands for. CI now pins the chart and asserts the rendered topology
matches the image.

---

## The interface

The visual language follows Uber's Base system: black-dominant, very high
contrast, hierarchy carried by type weight and size rather than by colour, flat
surfaces separated by whitespace and hairlines instead of boxes, and full-width
primary actions.

That language and the Apple HIG agree more than they disagree, which is why
both hold at once. The HIG says to prefer heavier weights and to build
hierarchy from weight, size, and colour; Uber leads with weight and size, which
leaves colour free to mean exactly one thing. The HIG says to group with
negative space and separator lines; Uber groups with hairlines rather than
drawing a border around every row. High contrast serves both.

The rules the interface actually holds to:

**Colour never carries meaning alone.** Every status has a text label beside
it. Heatmap severity is an outline and a shape marker as well as a colour, and
every cell prints its own hour count, so the chart survives grayscale and
color-blind viewing.

**Contrast is measured, not eyeballed.** Every token clears 4.5:1 against the
surface it sits on, in light and dark, and `e2e/accessibility.spec.ts` asserts
it on every screen. Two real bugs came out of that check. The heatmap ramp
originally ran to a dark grey that dropped its own cell label to 3.94:1, so the
ramp now stops short of black and every step clears 6:1. And when the "Coming
up" block became a filled black card, `si-due-date` kept rendering black text
on it at 1:1 — Angular scopes a component's styles, so a parent selector cannot
reach in to correct it. Custom properties do inherit through that
encapsulation, so the component now reads `--due-fg` and `--due-muted` with
sensible defaults and the container sets them.

**Targets clear 48px on touch and 32px on pointer**, focus is never suppressed,
and `prefers-reduced-motion` and `prefers-contrast` are both honoured. The type
scale is in rem so OS font-size settings scale it.

**The palette is restrained.** Status hues sit one step back from full
saturation. This is a tool a student opens while anxious about their term, and
an ordinary busy week should not look like an emergency.

Navigation follows each form factor: a top bar on desktop, a bottom tab bar on
small screens where the top of a phone is out of thumb reach.

### The review screen

`apps/web/src/app/review/` is the screen the product rests on, and §10.2 gives
it a hard target of p50 under 90 seconds for five courses. It orders by
confidence ascending, pre-accepts and collapses the high-confidence majority,
and requires a deliberate action only on low-confidence rows. Every row shows
the syllabus sentence it came from with its page number, so a student verifies
by glancing rather than reopening the PDF.

Building it surfaced a gap: the composite confidence score from §10.1 was
computed by the pipeline but never returned by the API, so the client was
guessing from date precision. It now comes from the server. Only the pipeline
can see a weekday mismatch or a failed validation rule, and those are precisely
the rows that must not slip into the auto-accepted group.

---

## Layer 2: course search

The PRD names one query in §14.3 and says to verify it before calling P2 done —
*"3-credit humanities elective, no group project, no attendance policy, papers
instead of exams."* It works, and `test_search.py` asserts it end to end.

```bash
python scripts/aggregate_corpus.py --dry-run
```

Aggregation is gated (§14.2): a profile publishes only when a human reviewed the
document, the grade weights validate, and the instructor has not opted out. The
skip reasons the script prints double as the corpus-coverage worklist — they say
exactly what each unpublished section is missing.

Profiles are **course + instructor + term** and are never averaged across
instructors (§14.1); every result states its provenance and age; and courses with
no syllabus still appear with an `unknown` profile, because §14.4 is right that
the empty state is the acquisition surface. One judgment call worth naming: when
a student filters on something only a profile can answer, unprofiled courses are
excluded — a course with no data has not been *shown* to have no group project.

---

## The parts that matter

**`packages/core/date_resolver/`** — the hardest part of the product, and pure
functions with no I/O so every row of §9.2's resolution table is unit-testable
against hand-constructed cases. The governing rule is §9.3: an ambiguity is
displayed as an ambiguity. `week_only` never collapses to a specific day, `tbd`
never reaches a calendar, and a time the source did not state is always marked
inferred.

**`services/worker/tasks/validate.py`** — `source_span_is_findable` is the
cheap, exact hallucination check that §16 gives a hard 0.5% ceiling. If the
model cannot quote the document, the model made it up, and the item drops to the
`low` confidence bucket no matter how confident the model claimed to be.

**`packages/core/workload_model/flags.py`** — §11.4's boundaries are written
with explicit `>=` / `<` because the PRD says so and gives the reason. The flag
explanations are product copy, not debug output; the PRD identifies that sentence
as the growth mechanism. Two details follow from that: crunch flags name their
own rolling 7-day span (so a badge reading "about 14 hours" doesn't appear to
contradict the "8.9h" on its cell), and cross-course collisions count courses
rather than summing percentages that don't share a denominator.

**`services/api/calendar_sync.py`** — sync is a diff, not a rewrite. The rule
that matters most is what happens when an event 404s on update: that means the
student deleted it, so it becomes a local dismissal and is never recreated. The
PRD calls recreating a deleted event "the fastest way to get uninstalled", and
`test_calendar_sync.py` asserts it stays dismissed across later syncs.

**`services/api/account.py`** — §15.2 and §12 pull in opposite directions here:
delete everything belonging to the student, but never garbage-collect the
corrections log. Both hold — the correction row survives with `user_id` and
`assessment_id` cleared, keeping the eval data while removing the link to a
person. Remote cleanup failures are reported, not fatal: a student who asked to
be deleted must not be blocked by Google being unreachable.

**`services/worker/tasks/persist.py`** — a section-level parse is *shared*
across everyone who uploaded the same syllabus. Re-parsing replaces the derived
rows and never touches `user_overrides`; if it cannot do that safely it refuses
loudly rather than silently discarding a student's correction. `corrections_log`
outlives the assessments it references, because §12 says never garbage-collect
it and everything that makes a correction useful for eval is on the row itself.

---

## Testing

```bash
pytest
```

The API tests need Postgres running and skip cleanly without it.

The one that matters most runs against the real stack. The Python suite stubs
the queue and the browser suite stubs the API, and every MVP-blocking bug so
far lived in the gap between them: a worker with no task registered, a review
screen that never polled, an emailed link the router did not serve. This drives
sign-in through heatmap over HTTP, with the model replaced by a recorded
extraction:

```bash
docker compose -f infra/docker/docker-compose.yml up -d
SMOKE_API_URL=http://localhost:8100 pytest tests/smoke -q
```

It runs in CI on every push.

Browser tests run against a stubbed API, so they need no database, worker, or
object store:

```bash
npm --prefix apps/web run e2e
```

They cover the upload through review to timeline flow that §27 asks for, on
both a desktop and a phone viewport, and they carry the accessibility rules as
assertions: contrast minimums on every screen in both appearances, target
sizes, heading order, accessible names, focus visibility, reduced motion, and a
check that no saturated colour has crept into the palette.

Those checks earn their keep. Writing them found four real bugs that had
survived manual review: two screens used `routerLink` without importing
`RouterLink`, so those links silently navigated nowhere; `new Date('2026-09-07')`
parses as UTC and rendered every heatmap week label a day early in the Americas;
`.btn--sm` had a 32px base with a *smaller* pointer override, inverting the
intended 44px touch floor; and the bottom tab bar's colour lived inside a media
query, leaving the links at the browser default blue outside it.

The eval gate is excluded from the default run because it costs money:

```bash
pytest tests/golden_set -m eval -rs
```

It skips loudly while `tests/golden_set/labels/` is empty. §16 requires 100
hand-labeled *real* syllabi before that number means anything — the fixtures in
`tests/fixtures/syllabi/` are synthetic and explicitly are not the golden set.

---

## Before this can go in front of students

1. **Answer PRD open questions 1–3.** Whether the institution publishes syllabi
   publicly changes the corpus strategy completely, and §14.4 calls seeding from
   a public archive the highest-leverage task in the project.
2. **Collect the golden set.** The eval gate is scaffolding until it exists.
3. **Replace the seeded calendar with real registrar data.** The Fall 2026
   fixture is invented. §9.1 is right that this is a hard dependency.
4. **Run one real extraction.** Set `LLM_MODE=live` and `LLM_API_KEY` in
   `.env`; the compose worker defaults to `replay`, which serves recorded
   extractions for the bundled fixtures so the whole loop runs with no key. The two-pass call is wired and cascades
   sonnet → opus per §25.3, but has never executed against the live API — no
   key was available while building. Everything downstream of it is verified.
5. **Exercise Google Calendar against real Google.** The OAuth flow, event
   diffing, and deletion handling are built and tested against a fake client,
   but no `GOOGLE_CALENDAR_CLIENT_ID` existed here. Refresh tokens are sealed
   at rest with a key Terraform generates into Secret Manager
   (`services/api/token_vault.py`).
6. **Start Google Calendar OAuth verification early** — §19 notes it takes weeks.
7. **Talk to the provost's office** before launch, not after (§15.3).

---

## Deploying

[DEPLOY.md](DEPLOY.md) is the runbook. The short version:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in
cp backend.hcl.example backend.hcl             # fill in
terraform init -backend-config=backend.hcl && terraform apply
```

Then set the repository secrets and variables `terraform output` prints, and
push to `main`. The deploy runs CI, builds four images tagged by commit, runs
migrations as their own job, rolls a Cloud Run revision, restarts the worker
VM, and publishes the app to Cloudflare Pages, in that order, so a failure at
any step leaves the previous version serving.

Until Terraform has run, the Deploy workflow detects that nothing is
provisioned and skips rather than failing, because a Deploy that is red for a
reason nobody can act on stops being a signal.

Authentication to GCP is Workload Identity Federation scoped to this one
repository. There is no service-account key anywhere in the pipeline.

---

## Layout

See PRD §21. The rule: if a change touches the extraction schema, the DB model,
or a prompt, it touches `packages/core` and reaches `api`, `worker`, and
`pipeline` by import — never by copy-paste.

```
packages/core/       schemas, db models, date_resolver, workload_model, prompts
services/api/        FastAPI, HTTP surface only, no parsing logic
services/worker/     Celery tasks: ingest, extract, resolve, validate
services/pipeline/   dbt models and the Airflow DAG that builds the corpus
apps/web/            Angular front end and the Playwright suite
scripts/             seed, fixture generation, pipeline runner
tests/               fixtures, golden set, end-to-end tests
infra/docker/        local dev stack and the images the cluster runs
infra/terraform/     Cloud Run, GKE, Neon, R2, Pages, DNS
infra/k8s/           manifests for the always-on workloads, plus Airflow's Helm values
.github/workflows/   CI, the deploy pipeline, and the eval gate
```
