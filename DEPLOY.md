# Deploying

Everything here is automated except the parts that need an account only you can
open or a payment method only you can enter. Those are marked **you**. The rest
runs from `terraform apply` and a push to `main`.

Read this once before starting. Step 3 generates credentials that step 4 needs,
and doing them out of order means a second apply.

---

## What gets deployed where

| Piece | Runs on | Why there |
|---|---|---|
| Angular app | Cloudflare Pages | Static files, free, already in the zone doing rate limiting |
| FastAPI | Cloud Run, scales to zero | Two busy weeks a year; a warm instance the rest of the time buys nothing |
| Celery workers, Redis, MLflow, Airflow | GKE, one `e2-standard-2` node | A Celery worker long-polls its broker, and Cloud Run throttles CPU outside requests, so a worker there starves |
| Postgres | Neon | The only free tier that survives a seasonal workload; Supabase pauses a project after a week idle |
| Syllabus files | Cloudflare R2 | Free egress, S3 API, so the same boto3 code runs against LocalStack locally |

Recurring cost is the node pool at roughly six to ten dollars a month plus
model tokens. Everything else sits inside a free tier, including the GKE
control plane: one zonal cluster per billing account carries a credit that
covers it, which is why the cluster is zonal and why a second one would not
be free. Section 18.5 of the PRD costs a
four-month term at 50 to 105 dollars for about 500 students.

---

## 1. Accounts (**you**)

Three, all with a card on file even where the tier is free:

- **Google Cloud**: create a project, note the project ID, enable billing.
- **Cloudflare**: add the domain as a zone. It must be a zone in this account
  before Terraform runs, because `dns.tf` looks it up rather than creating it.
- **Neon**: create an account and an API key. Terraform creates the project.

Also needed: a transactional email provider (Brevo's free tier is 300 a day,
which is enough until it isn't) and an Anthropic API key.

## 2. Enable the GCP APIs (**you**)

An apply against a project with these disabled fails one resource at a time,
which is a slow way to find out.

```bash
gcloud services enable \
  run.googleapis.com \
  compute.googleapis.com \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  --project YOUR_PROJECT_ID
```

## 3. State bucket (**you**, once)

State holds the Neon connection string, the generated JWT signing key, and the
R2 secret in cleartext. Versioning is on so a bad apply is recoverable.

```bash
gsutil mb -l us-central1 gs://YOUR-tfstate-bucket
gsutil versioning set on gs://YOUR-tfstate-bucket
```

## 4. Fill in the variables

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
$EDITOR terraform.tfvars backend.hcl
```

Both files are gitignored. Four of the values in `terraform.tfvars` are
credentials.

The Cloudflare token needs three scopes: **R2 edit**, **Zone DNS edit**, and
**Cloudflare Pages edit**. A token missing the Pages scope fails late, after
DNS records already exist.

## 5. Apply

```bash
terraform init -backend-config=backend.hcl
terraform plan     # read it
terraform apply
```

The first apply starts Cloud Run on Google's public `hello` container, because
neither the service nor the migration job can be created against an image that
does not exist yet, and on a fresh project none do. The first deploy replaces
it, and from then on CI owns the image tag and Terraform ignores it. The cluster
comes up with a node pool and no workloads, which is also fine: the deploy
applies them.

## 6. Wire up GitHub

One command, from the same machine that ran Terraform:

```bash
export CLOUDFLARE_API_TOKEN=...   # the token from terraform.tfvars
scripts/configure_github.sh --dry-run
scripts/configure_github.sh
```

It reads every value from `terraform output` and sets four repository
variables and four secrets with the GitHub CLI. Secrets go to `gh` on stdin, so
they never appear in the process list or shell history. It reads all eight
before writing any, so a missing output cannot leave the repository
half-configured.

That matters because the Deploy workflow refuses to start on a partial set.
With nothing configured it skips quietly; with everything it deploys; with
some but not all it fails immediately and names what is missing, rather than
migrating the production database and then failing at the last step.

There is no service-account JSON key anywhere in this, and that is deliberate.
A JSON key is a permanent credential sitting in a GitHub secret; nothing about
it expires, so a leak keeps working until a human notices. Workload Identity
Federation mints a token per run, scoped by `attribute_condition` to exactly
one repository.

## 7. Deploy

Push to `main`, or start it by hand with `gh workflow run Deploy`. The order is:

```
CI  ->  build and push images  ->  migrate  ->  API revision  ->  cluster  ->  web app
```

The cluster step installs External Secrets Operator and KEDA (both ship CRDs
the manifests reference, so they go first and the install waits), applies
`infra/k8s/`, runs the Airflow Helm chart, then blocks on every rollout and
asks the worker whether it actually registered the task the API enqueues.

Each step leaves the previous version serving if it fails. Migrations run
before the new revision exists, so the schema is always at or ahead of the code
reading it. That is only safe because migrations are additive: a revision still
serving must keep working against the new schema. **Dropping a column is two
deploys, never one.**

## 8. Email deliverability (**you**, before the first real signup)

Sign-in is a magic link, so mail delivery *is* authentication. Section 18.4 is
blunt about this: if links land in spam during syllabus week, signups fail and
nobody reports it. Set SPF, DKIM and DMARC on the sending domain, then send
yourself one to a real `.edu` inbox and confirm it arrives in the inbox rather
than in spam.

## 9. R2 lifecycle rule (**you**, out of band)

The Cloudflare Terraform provider v4 has no resource for this, so it stays a
manual command. Originals are read once at parse time and then almost never,
which is exactly what Infrequent Access is priced for, and the 10 GB free tier
runs out around ten thousand documents.

```bash
wrangler r2 bucket lifecycle add syllint-production-syllabi \
  --prefix documents/ --transition-days 30 --storage-class InfrequentAccess
```

---

## Rolling back

Images are tagged by commit, never `latest`, so a rollback is a redeploy of a
known tag rather than a rebuild:

```bash
gcloud run deploy syllint-production-api \
  --region us-central1 \
  --image us-central1-docker.pkg.dev/PROJECT/syllint/api:GOOD_SHA
```

On the cluster, a rollback is a rollout undo rather than a redeploy:

```bash
kubectl -n syllint rollout undo deployment/worker
helm -n syllint rollback airflow
```

Or, faster, split traffic back to the previous revision:

```bash
gcloud run services update-traffic syllint-production-api \
  --region us-central1 --to-revisions PREVIOUS=100
```

**The database does not roll back with it.** Alembic has a `downgrade`, but a
downgrade that drops a column is not a rollback, it is data loss. If a
migration is the problem, the fix is a new forward migration.

Artifact Registry keeps the ten most recent images regardless of age, and
deletes tagged images older than thirty days. Rolling back further than that
means rebuilding from the tag in git.

---

## Checking a deploy actually worked

The workflow already asserts these. Run them by hand when something looks off.

```bash
# The API is up and knows which environment it is.
curl -s https://api.YOUR_DOMAIN/health

# The app is pointed at the right API. This is the one that breaks silently:
# a stale config.json means every request 404s against the static host and the
# app reports a parse error instead of a routing mistake.
curl -s https://YOUR_DOMAIN/config.json

# A route with no file behind it must still return the app, not a 404.
curl -sI https://YOUR_DOMAIN/timeline | head -1
```

---

## Known gaps

Listed because a runbook that hides them is worse than no runbook.

- **The live model call has never run.** No API key was available while this was
  built, so extraction is exercised against a stubbed client. The first real
  upload is the first real test of the two-pass prompt.
- **The golden set is empty.** Section 16 asks for 100 syllabi with known
  answers, and the hallucination ceiling of 0.5% is unmeasured until they exist.
  The nightly **Extraction eval** workflow reports this as a skipped job with
  a notice, rather than running a suite with nothing in it and passing. Add
  labels to `tests/golden_set/labels/` and an `LLM_API_KEY` repository secret,
  and it starts gating for real.
- **The browser suite stubs the API.** `tests/smoke` closes most of that gap
  by driving the loop over HTTP against the compose stack in CI; the Angular
  templates themselves are still only exercised against stubs.
- **The cluster is a single-node zonal one.** A node upgrade or a zone
  incident is downtime. The queue survives it in Redis, and it is a node-pool
  size change rather than a redesign when that stops being acceptable.
- **Nodes have public egress.** Private nodes need a Cloud NAT gateway, which
  is billed at several times the node pool it would serve. Nothing in the
  cluster listens publicly; see the comment in `infra/terraform/gke.tf`.
