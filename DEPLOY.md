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
| Celery workers, Redis, MLflow, Airflow | One `e2-standard-2` VM | A Celery worker long-polls its broker, and Cloud Run throttles CPU outside requests, so a worker there starves |
| Postgres | Neon | The only free tier that survives a seasonal workload; Supabase pauses a project after a week idle |
| Syllabus files | Cloudflare R2 | Free egress, S3 API, so the same boto3 code runs against MinIO locally |

Recurring cost is the VM at roughly six to ten dollars a month plus model
tokens. Everything else sits inside a free tier. Section 18.5 of the PRD costs a
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

The four `*_image` variables point at tags that do not exist yet. That is fine
for the first apply: Cloud Run will fail to pull, the service still gets
created, and the first deploy replaces the tag. The worker VM is the same
story, and a `gcloud compute instances reset` after the first deploy brings it
up properly.

## 6. Wire up GitHub

`terraform output` prints everything the workflow needs.

Repository **secrets**:

| Secret | From |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `terraform output -raw workload_identity_provider` |
| `GCP_SERVICE_ACCOUNT` | `terraform output -raw deploy_service_account` |
| `CLOUDFLARE_API_TOKEN` | the token from step 4 |
| `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare account id |

Repository **variables** (not secrets; they end up in the built app anyway):

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | your project id |
| `GCP_REGION` | `us-central1` |
| `ARTIFACT_REPOSITORY` | `syllint` |
| `APP_DOMAIN` | the domain from step 1 |

There is no service-account JSON key anywhere in this list, and that is
deliberate. A JSON key is a permanent credential sitting in a GitHub secret;
nothing about it expires, so a leak keeps working until a human notices.
Workload Identity Federation mints a token per run, scoped by
`attribute_condition` to exactly one repository.

## 7. Deploy

Push to `main`, or run the **Deploy** workflow by hand. The order is:

```
CI  ->  build and push images  ->  migrate  ->  API revision  ->  worker VM  ->  web app
```

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
- **The browser suite stubs the API.** `tests/smoke` closes most of that gap
  by driving the loop over HTTP against the compose stack in CI; the Angular
  templates themselves are still only exercised against stubs.
- **Airflow runs `standalone`.** Fine for one box behind a firewall, not a
  production Airflow deployment.
