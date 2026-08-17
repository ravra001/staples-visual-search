# GCP Setup & Deployment Runbook

Everything needed to take this app from zero to a live Cloud Run deployment
backed by Cloud SQL (Postgres + pgvector), written from real experience
deploying this exact project. Every gotcha in the "Troubleshooting" section
actually happened during that deployment — this isn't theoretical.

**Do this in Cloud Shell, not your local machine.** See
[Why Cloud Shell, not local `gcloud`](#why-cloud-shell-not-local-gcloud)
before you start.

---

## 0. Naming conventions used in this guide

Pick your own values or use these. Every script below uses these variables —
set them once in your Cloud Shell session and everything else copy-pastes
cleanly.

```bash
export PROJECT_ID="staples-visual-search-demo"   # must be globally unique —
                                                   # append a random suffix
                                                   # (e.g. -482) if taken
export REGION="us-central1"
export SQL_INSTANCE="staples-pgvector"
export DB_NAME="staples"
export DB_USER="staples_app"
export SERVICE_NAME="staples-visual-search"
export AR_REPO="staples-repo"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"
export SA_NAME="staples-run-sa"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
```

Every later step assumes these are already exported in your shell. If you
open a new Cloud Shell tab, re-export them (or put this block in a file and
`source` it).

---

## 1. Create the project

```bash
gcloud projects create "$PROJECT_ID" --name="Staples Visual Search"
gcloud config set project "$PROJECT_ID"

# Link billing — replace with your actual billing account ID
# (find it with: gcloud billing accounts list)
export BILLING_ACCOUNT_ID="XXXXXX-XXXXXX-XXXXXX"
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT_ID"
```

---

## 2. Enable required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  compute.googleapis.com
```

This takes a minute or two the first time.

---

## 3. IAM — a dedicated service account for Cloud Run

Don't run the service as the default compute service account (overly broad
permissions). Create a scoped one instead.

```bash
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Staples Visual Search — Cloud Run runtime"

# Cloud SQL client — lets Cloud Run connect via the Unix-socket connector
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/cloudsql.client"

# Secret Manager accessor — lets Cloud Run read the DB password at deploy time
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 4. Provision Cloud SQL for Postgres

pgvector ships as a built-in extension on Cloud SQL Postgres 15+ — no custom
image or manual install needed, just `CREATE EXTENSION vector` later.

```bash
gcloud sql instances create "$SQL_INSTANCE" \
  --database-version=POSTGRES_16 \
  --region="$REGION" \
  --tier=db-custom-2-8192 \
  --storage-size=20 \
  --storage-auto-increase \
  --availability-type=zonal
```

Sizing notes:
- `db-custom-2-8192` = 2 vCPU / 8GB RAM. Fine for a ~10k-row catalog. Scale up
  if your catalog is much larger or you need higher QPS.
- `--availability-type=zonal` is cheaper than `regional` (no HA standby) —
  fine for a demo, bump to `regional` for real production.

Create the database and app user:

```bash
gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"

# Generate a strong password and store it DIRECTLY in Secret Manager —
# never print it to the terminal or write it to a file on disk.
openssl rand -base64 24 | tr -d '\n' | \
  gcloud secrets create staples-db-password --data-file=-

gcloud sql users create "$DB_USER" \
  --instance="$SQL_INSTANCE" \
  --password="$(gcloud secrets versions access latest --secret=staples-db-password)"
```

Grant the Cloud Run service account access to that secret (already done in
step 3, repeated here since it's the same secret this step just created —
no-op if you ran step 3 after this).

### Network access

For Cloud Run, you don't need a public IP or authorized networks at all —
the Cloud SQL Unix-socket connector (wired up in the Cloud Run deploy step
below via `--add-cloudsql-instances`) reaches the instance over Google's
private network. Skip public IP entirely unless you also need to connect
from your own machine or Cloud Shell directly (e.g. for the seeding step
below, or manual `psql` access):

```bash
# Only if you need direct access from Cloud Shell / your machine:
gcloud sql instances patch "$SQL_INSTANCE" --assign-ip

# Then authorize Cloud Shell's current IP (changes each session — re-run
# this if you get a connection-refused error in a later step):
export MY_IP=$(curl -s ifconfig.me)
gcloud sql instances patch "$SQL_INSTANCE" \
  --authorized-networks="${MY_IP}/32"
```

Get the connection details you'll need later:

```bash
export SQL_CONNECTION_NAME=$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')
export SQL_PUBLIC_IP=$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses[0].ipAddress)')
echo "Connection name: $SQL_CONNECTION_NAME"
echo "Public IP:        $SQL_PUBLIC_IP"
```

---

## 5. Artifact Registry — where the built image lives

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Staples Visual Search images"
```

---

## 6. Get the code into Cloud Shell

```bash
mkdir -p ~/staples-deploy && cd ~/staples-deploy
```

Upload the repo here — either `git clone` if it's on GitHub, or use Cloud
Shell's **⋮ menu → Upload** for a zipped/tarred copy if it's local-only.
Confirm you have `Dockerfile`, `backend/`, `frontend/`, `requirements.txt`,
`requirements-ml.txt` at the top level.

Stage the source in GCS once — this is what every future build references,
so you never have to re-upload the (potentially 1GB+, with the bundled CLIP
model and product images) source tree again after this:

```bash
gsutil mb -l "$REGION" "gs://${PROJECT_ID}-deploy-src" 2>/dev/null || true
tar -czf /tmp/source.tar.gz --exclude='.git' .
gsutil cp /tmp/source.tar.gz "gs://${PROJECT_ID}-deploy-src/staples-deploy-source.tar.gz"
```

This first upload does go through Cloud Shell's own (sometimes slow)
network — budget several minutes for it if your source tree includes the
bundled model + product images. See
[Troubleshooting: slow/stuck uploads](#slowstuck-uploads-from-cloud-shell)
if it seems to hang. Every later rebuild in this guide reuses this same GCS
object as the build source and does **not** re-upload it — Cloud Build
fetches straight from GCS internally.

---

## 7. Seed the database

This step needs direct DB access from Cloud Shell (the public-IP + authorized-
networks setup from step 4). Point the app at the SQL backend and run the
seed function:

```bash
cd ~/staples-deploy/backend
pip install -r ../requirements.txt -r ../requirements-ml.txt

export DATABASE_URL="postgresql+psycopg://${DB_USER}:$(gcloud secrets versions access latest --secret=staples-db-password)@${SQL_PUBLIC_IP}:5432/${DB_NAME}"
export DATA_BACKEND=sql
export CATALOG_FILE=data/catalog_abo.json   # or your own catalog

python -c "
import products_repo_sql as r
r.init_and_seed(reuse_vectors_from='data/index_clip.npz')
"
```

`reuse_vectors_from` points at a prebuilt `index_*.npz` (see
`build_index.py`) so seeding reuses already-computed CLIP vectors instead of
re-embedding the whole catalog against Postgres — seeding ~10k rows this way
takes under 3 minutes. If you don't have a prebuilt index yet, pass
`embed_missing=True` instead (much slower — one model call per product).

Sanity-check it worked:

```bash
python -c "
import products_repo_sql as r
print('categories:', r.get_categories())
print('total:', r.count_by_category())
"
```

---

## 8. Build the Docker image

Cloud Run needs the image built and pushed to Artifact Registry. This
`Dockerfile` (repo root) is the one validated by this project — the critical
line is installing `torch` and `torchvision` from the **same** CPU-only
index in the **same** command (see
[Troubleshooting: torchvision::nms](#torchvisionnms-does-not-exist)):

```dockerfile
FROM python:3.13-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-ml.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ENV HOST=0.0.0.0
ENV EMBEDDING_BACKEND=clip
ENV DATA_BACKEND=sql
ENV CATALOG_FILE=data/catalog_abo.json
# DATABASE_URL is supplied at deploy time via Secret Manager — never baked
# into the image.

WORKDIR /app/backend
EXPOSE 8080
CMD ["python", "run.py"]
```

Build it, referencing the GCS-staged source from step 6 (fast — no re-upload):

```bash
gcloud builds submit "gs://${PROJECT_ID}-deploy-src/staples-deploy-source.tar.gz" \
  --tag "$IMAGE"
```

**Redeploying after a code change?** Don't repeat the slow local tarball
re-upload. See
[Rebuilding without re-uploading](#rebuilding-without-re-uploading-the-full-source)
below — patch the GCS-staged source in-flight via a small `cloudbuild.yaml`
step instead.

---

## 9. Store the DB connection as a secret Cloud Run can read

Cloud Run needs `DATABASE_URL` built with the Unix-socket path (not a public
IP — the private connector is faster and doesn't need authorized networks):

```bash
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=staples-db-password)
printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' \
  "$DB_USER" "$DB_PASSWORD" "$DB_NAME" "$SQL_CONNECTION_NAME" | \
  gcloud secrets create staples-database-url --data-file=-
```

Grant the Cloud Run service account access to this secret too:

```bash
gcloud secrets add-iam-policy-binding staples-database-url \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 10. Deploy to Cloud Run

These flags reflect real production tuning learned the hard way — see
[Troubleshooting](#slow-first-request--intermittent-search-failed) for why
`min-instances` and `concurrency` matter here.

```bash
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$SQL_CONNECTION_NAME" \
  --set-secrets "DATABASE_URL=staples-database-url:latest" \
  --min-instances=1 \
  --max-instances=10 \
  --memory=4Gi \
  --cpu=2 \
  --concurrency=20 \
  --timeout=300 \
  --allow-unauthenticated
```

Flag notes:
- `--min-instances=1` — keeps one container warm at all times. Without this,
  Cloud Run scales to zero when idle and every request after a quiet period
  eats a ~25s cold start (loading torch + CLIP into memory), which reads as
  random "Search failed" errors in the UI when a request lands mid-startup.
  Costs a small continuous baseline charge — worth it for anything beyond a
  throwaway demo.
- `--memory=4Gi` — 2Gi is tight once torch/CLIP/pgvector are all loaded plus
  headroom for concurrent inference; 4Gi gives real margin against OOM kills.
- `--concurrency=20` (well below the default 80) — CLIP inference is
  CPU-bound and holds the GIL; too many simultaneous requests on one warm
  instance starve each other for CPU rather than actually running in
  parallel. 20 keeps that from happening on a 2-vCPU instance.
- `--allow-unauthenticated` — public demo. Drop this and use IAM invoker
  bindings for anything that shouldn't be publicly reachable.

Get the live URL:

```bash
gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)'
```

---

## 11. Verify

```bash
export URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)')

curl -s "$URL/api/config"
curl -s "$URL/api/categories"
curl -s -o /dev/null -w "products: %{time_total}s\n" "$URL/api/products"

# Visual search — swap in any product image
curl -s -X POST "$URL/api/visual-search" -F "file=@backend/static/images/products/<some-sku>.jpg" \
  | python -m json.tool
```

All of these should return in well under a second once the instance is warm
(`min-instances=1` means it always should be).

---

## 12. Continuous deployment via GitHub

Once the repo has a GitHub remote, replace the manual "rebuild in Cloud Shell
every time" loop above with a Cloud Build trigger: push to `main`, and Cloud
Build builds + deploys automatically. The repo's `cloudbuild.yaml` defines
the build; you only need to connect it once.

**IAM — let Cloud Build actually deploy** (Cloud Build's own service account
needs rights to call the Cloud Run API and to act as the runtime service
account):

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/run.admin"

export RUNTIME_SA=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/iam.serviceAccountUser"

# Also needs read access to the GCS-staged model (see the gotcha below)
gsutil iam ch "serviceAccount:${CLOUDBUILD_SA}:objectViewer" \
  "gs://${PROJECT_ID}-deploy-src"
```

**Connect the repo** (Cloud Console → Cloud Build → Triggers → Connect
Repository → GitHub → authorize → select the repo). This step is inherently
a browser OAuth flow; there's no clean CLI-only path for the initial
connection.

**Create the trigger**: event = push to branch `^main$`, configuration =
**"Cloud Build configuration file (YAML or JSON)"**, location = `/cloudbuild.yaml`.

⚠️ **Do not pick "Autodetected" / "Dockerfile" here**, even though it looks
like the simpler option and the console may suggest it first. That path
skips this repo's `cloudbuild.yaml` entirely and instead deploys through its
own wizard, which creates or updates a Cloud Run service using its own
defaults — a different region than the rest of your infrastructure, the
project's *default* Compute Engine service account (not the one IAM was
configured for in step 3), and none of the Cloud SQL connection, secrets,
or scaling flags from step 10. See
[Trigger silently deployed a second, broken service](#trigger-silently-deployed-a-second-broken-service)
below for exactly what this looks like when it happens, and how to confirm
which configuration a trigger is actually using
(`gcloud builds triggers list --format="table(name,filename)"` — an empty
`filename` column means it's on the wizard path, not your `cloudbuild.yaml`).

From then on, `git push origin main` is the entire deploy process.

---

## 13. Product images via GCS + Cloud CDN

Right now, product images can be served straight from the Cloud Run
container's own filesystem — simple, but they then compete with CLIP
inference for the same container's CPU/concurrency budget on every catalog
page load. Moving them to a GCS bucket fronted by a real Cloud CDN (an
external HTTPS load balancer with a backend bucket) removes that contention
entirely, and this app already has the plumbing for it: set
`IMAGES_BASE_URL` and every product-image request 302s to the CDN instead
(see `backend/main.py`'s `redirect_product_image`) — no other code needs to
change, since every image reference in the app (API-provided or the
homepage's hardcoded sample thumbnails) already goes through the same
same-origin relative path.

**Create the bucket and make objects public** (product photos on a shopping
site — non-sensitive, meant to be public assets):

```bash
export IMAGES_BUCKET="${PROJECT_ID}-product-images"

gsutil mb -l "$REGION" "gs://${IMAGES_BUCKET}"
gsutil uniformbucketlevelaccess set on "gs://${IMAGES_BUCKET}"
gsutil iam ch allUsers:objectViewer "gs://${IMAGES_BUCKET}"
```

**Get the images into the bucket via git**, since they're tracked in the
repo (unlike the CLIP model) — no manual upload needed, just a clone
+ `gsutil cp`. This requires the GitHub repo to be cloneable from Cloud
Shell with no stored credentials, i.e. **public** — if it's private, either
make it public (Settings → Danger Zone → Change visibility — fine for a
hackathon repo, and consistent with this project's "everything is real,
verify it yourself" framing) or use a Personal Access Token in the clone
URL instead:

```bash
cd ~ && rm -rf staples-images-src
git clone --depth=1 https://github.com/<you>/<repo>.git staples-images-src
cd staples-images-src/backend/static/images/products

# Real Cache-Control header -- GCS doesn't set one by default, and Cloud
# CDN only caches at the edge if the origin says to.
gsutil -m -h "Cache-Control:public, max-age=604800" cp -r . "gs://${IMAGES_BUCKET}/products/"
```

Safe to re-run if interrupted — `gsutil cp` overwrites by object name, so
running it twice just re-uploads the same files to the same paths with no
duplicates or errors.

**Backend bucket + Cloud CDN + HTTPS load balancer.** Getting a real HTTPS
cert normally means owning a domain — the trick below (`sslip.io`, a public
service that resolves `<ip-with-dashes>.sslip.io` to that literal IP) gets a
genuine Google-managed certificate without one:

```bash
# Backend bucket with CDN enabled
gcloud compute backend-buckets create staples-images-backend \
  --gcs-bucket-name="$IMAGES_BUCKET" --enable-cdn

# Reserve a global static IP
gcloud compute addresses create staples-images-ip --global
export LB_IP=$(gcloud compute addresses describe staples-images-ip --global --format='value(address)')

# Turn it into a hostname sslip.io will resolve back to that same IP
export CDN_HOST="$(echo $LB_IP | tr '.' '-').sslip.io"

# Google-managed SSL cert for that hostname
gcloud compute ssl-certificates create staples-images-cert \
  --domains="$CDN_HOST" --global

# URL map -> backend bucket -> HTTPS proxy -> forwarding rule
gcloud compute url-maps create staples-images-lb \
  --default-backend-bucket=staples-images-backend
gcloud compute target-https-proxies create staples-images-https-proxy \
  --url-map=staples-images-lb --ssl-certificates=staples-images-cert
gcloud compute forwarding-rules create staples-images-https-rule \
  --address=staples-images-ip --global \
  --target-https-proxy=staples-images-https-proxy --ports=443
```

**The managed cert takes 15–60 minutes to provision and validate** — this
is normal Google-side behavior, not something stuck. Poll it rather than
guessing when it's ready:

```bash
gcloud compute ssl-certificates describe staples-images-cert \
  --global --format='value(managed.status)'
# PROVISIONING -> ACTIVE
```

**Cut over only once it's `ACTIVE`** — verify the CDN actually serves an
image (`curl -sI https://$CDN_HOST/products/<some-sku>.jpg`) before setting
`IMAGES_BASE_URL=https://$CDN_HOST` anywhere real. Setting it while the cert
is still provisioning would 302 every product image at a TLS handshake that
isn't ready yet, breaking live images on an otherwise-working deployment.
Once verified, set it as an env var on the Cloud Run service (either add
`--set-env-vars=IMAGES_BASE_URL=https://$CDN_HOST` to the `gcloud run
deploy` step in `cloudbuild.yaml` so it survives every future CI deploy, or
apply it once with `gcloud run services update`).

---

## Troubleshooting

Every one of these actually happened during this project's deployment.

### Why Cloud Shell, not local `gcloud`

If your network runs TLS-intercepting security software (common on managed
corporate/personal Windows machines), local `gcloud auth login` can fail
with:

```
SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate
verify failed: Basic Constraints of CA cert not marked critical
```

This is OpenSSL strictly rejecting the intercepting proxy's root CA
certificate itself (a non-RFC-5280-compliant Basic Constraints extension) —
not a missing-certificate problem, so pointing `gcloud config set
core/custom_ca_certs_file` at a freshly exported CA bundle does **not** fix
it. There is no known client-side workaround. Use Cloud Shell (browser-based,
already authenticated, no local TLS involved) for all `gcloud`
build/deploy/auth work instead.

**`git`/GitHub over HTTPS hits a similar-looking error, but it IS fixable**
(`SSL certificate problem: unable to get local issuer certificate`) — switch
git's SSL backend from OpenSSL to Windows' native schannel, which validates
against the OS cert store instead of git's own bundled CA bundle:

```bash
git config http.sslBackend schannel
```

Don't assume git is blocked the same way `gcloud` is — it's a different
failure mode with a real local fix.

### Missing bundled model after switching to a GitHub-triggered build

```
Failed. Details: The user-provided container failed to start and listen on
the port defined provided by the PORT=8080 environment variable within the
allocated timeout.
```

`backend/models/` (the ~577MB bundled CLIP model) is `.gitignore`'d — it
exceeds GitHub's 100MB file limit. Earlier manual deploys packaged it from
local disk into a tarball that bypassed git entirely, so this never came up.
The moment the build source switches to a GitHub checkout (a CI trigger),
the model is simply absent from the image, and the app falls back to
downloading it from HuggingFace at cold start — slow enough (and dependent
enough on outbound network access from the build/runtime environment) to
blow past Cloud Run's startup health-check timeout, which surfaces as this
generic "failed to start and listen on port" error with no obvious link to
a missing model file.

Fix: stage the model in GCS once (console.cloud.google.com/storage — a
browser upload sidesteps local `gcloud`/`gsutil` auth entirely, which is
useful given the TLS issue above), then add steps to `cloudbuild.yaml` that
fetch it into the build context before `docker build` runs.

**Upload it as a single zip, not a folder.** The GCS console's folder-upload
UI silently dropped files on the first attempt at staging
`backend/models/hf/` directly (thousands of small HuggingFace cache files) —
`gsutil ls -r` on the result came back completely empty with no error
anywhere, and a `gsutil rsync` step pointed at that empty prefix "succeeds"
(copies zero files) rather than failing, so the build sails on to `docker
build` and fails at the exact same spot for what looks like the same reason.
Zip the model locally, upload that one file, and unzip it in the build
(`gsutil`'s cloud-builder image doesn't reliably have `unzip`, hence the
plain Python step):

```yaml
- name: 'gcr.io/cloud-builders/gsutil'
  args: ['cp', 'gs://BUCKET/model-cache/hf_model.zip', 'hf_model.zip']
- name: 'python:3.11-slim'
  entrypoint: 'python'
  args: ['-c', "import zipfile; zipfile.ZipFile('hf_model.zip').extractall('backend/models')"]
```

Grant Cloud Build's service account read access to that bucket (see step 12
above) — a silent permission failure on this step looks identical to the
model just not being there. After uploading, always verify with `gsutil ls
-lh gs://BUCKET/model-cache/hf_model.zip` that the size matches the local
file exactly before assuming the upload actually finished.

### Trigger silently deployed a second, broken service

Same generic error as above (`container failed to start ... within the
allocated timeout`), completely different cause, and easy to mis-diagnose
as a repeat of the missing-model issue since the symptom is identical.

When creating the trigger in step 12, the Cloud Build console offers an
**"Autodetected" / Dockerfile** configuration option alongside "Cloud Build
configuration file" — and may even suggest it as the default. Picking it
does not use this repo's `cloudbuild.yaml` at all. Instead it deploys
through its own wizard, using its own defaults: a **different region** than
the rest of the project's infrastructure, the project's **default Compute
Engine service account** (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`)
instead of the one IAM was configured for, and **none** of the Cloud SQL
connection, secrets, or `--min-instances`/`--concurrency` tuning from step
10. The result is a second Cloud Run service — same name, different region
— that has no way to reach the database and hangs on startup until the
health check times out.

Diagnose it directly rather than guessing:

```bash
# Does a service with the same name exist in an unexpected region?
gcloud run services list --region=us-east1   # or wherever it landed

# Is the trigger actually using cloudbuild.yaml? An empty filename column
# means no -- it's on the wizard path.
gcloud builds triggers list --format="table(name,filename)"
```

Fix: edit the trigger, change its configuration to "Cloud Build
configuration file (YAML or JSON)" pointing at `/cloudbuild.yaml`, then
delete the errant service in the wrong region
(`gcloud run services delete SERVICE_NAME --region=WRONG_REGION --quiet`) —
it never had a working database connection, so there's nothing on it worth
keeping.

### `git clone` from Cloud Shell prompts for a GitHub username/password

Cloud Shell has no stored GitHub credentials, and GitHub no longer accepts
real account passwords for git operations over HTTPS (a Personal Access
Token is required even if you have one). This isn't a Cloud Shell
misconfiguration — it means the repo is **private**. Either make it public
(Settings → Danger Zone → Change visibility — reasonable for a hackathon
repo you want reviewable anyway) or clone with a PAT embedded in the URL:
`git clone https://<token>@github.com/<you>/<repo>.git`.

### `torchvision::nms does not exist`

```
{"detail":"Could not process image: operator torchvision::nms does not exist"}
```

Root cause: `torch` installed from the CPU-only PyPI index
(`--index-url https://download.pytorch.org/whl/cpu`), but `torchvision`
installed afterward (or in a separate pip invocation) resolves from default
PyPI and pulls a CUDA build that doesn't match. Fix: install both from the
same CPU index in the same `pip install` command (see the Dockerfile in
step 8).

### Slow first request / intermittent "Search failed"

Two causes, usually together:
1. No `--min-instances` set → Cloud Run scales to zero when idle → every
   request after a quiet period is a ~25s cold start (loading torch+CLIP).
   Fix: `--min-instances=1`.
2. Default `--concurrency=80` lets many CLIP inference calls pile onto one
   instance and starve each other for CPU. Fix: lower `--concurrency` (20
   worked well for a 2-vCPU instance here).

The frontend surfaces both of these as a generic "Search failed" message
(it only shows a specific reason when the API itself returns one; a Cloud
Run gateway timeout/502 has no such body).

### pgvector HNSW misses obvious matches

If an exact brute-force/in-memory search finds an obviously-correct result
but the SQL/pgvector-backed search doesn't (returns unrelated items or "no
strong matches"), it's the ANN index being *approximate*: pgvector's default
`hnsw.ef_search=40` doesn't explore enough of the graph for catalogs in the
few-thousand-to-low-tens-of-thousands row range, and can get stuck in the
wrong neighborhood entirely for queries near a cluster boundary. Fix: raise
`ef_search` at query time (no reindex needed):

```sql
SET LOCAL hnsw.ef_search = 200;
```

Do this inside the same transaction as your `ORDER BY <=>` query (`SET
LOCAL` scopes it to that transaction only, so it can't leak into unrelated
queries on the same connection pool). At catalog sizes under ~50k rows, the
latency cost of a wider search is negligible; correctness matters more than
shaving a few milliseconds off an ANN search that's already fast.

### Listing endpoints are slow (multi-second `/api/products`, `/api/categories`)

If these endpoints call a `get_all_products()`-style function that fetches
every row (every column, including long description text) just to slice one
page or extract a handful of distinct values in Python afterward, push both
down into SQL instead:

```python
def get_categories():
    with SessionLocal() as s:
        rows = s.execute(select(Product.category).distinct().order_by(Product.category)).all()
        return [r[0] for r in rows]

def get_products_page(category=None, limit=24, offset=0):
    with SessionLocal() as s:
        base = select(Product)
        if category is not None:
            base = base.where(Product.category == category)
        total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        rows = s.execute(base.order_by(Product.sku).limit(limit).offset(offset)).scalars().all()
        return total, [p.as_dict() for p in rows]
```

This took `/api/products` from ~8s to ~2s even over a cross-region test
connection; same-region (Cloud Run → Cloud SQL) it's sub-200ms.

### N+1 round trips while seeding

A per-row `session.get(Product, sku) or Product(sku=sku)` existence check
before insert is fine against `localhost` but is a *separate network round
trip per row* against a remote Cloud SQL instance — 10,000 rows at even
100-200ms RTT is 15-30+ minutes before a single row is written. Fetch the
existing SKU set **once**, then batch inserts/updates (1000-row batches):

```python
existing_skus = {row[0] for row in s.query(Product.sku).all()}
to_insert = [r for r in rows if r["sku"] not in existing_skus]
to_update = [r for r in rows if r["sku"] in existing_skus]
for i in range(0, len(to_insert), 1000):
    s.execute(Product.__table__.insert(), to_insert[i:i+1000])
```

### Duplicate SKUs in the source catalog

Postgres enforces the primary key uniqueness a Python list silently doesn't.
If your catalog source has duplicate IDs, dedupe deterministically
(keep-last-occurrence) before inserting, and log the count so it's visible
rather than silently dropping data:

```python
before = len(rows)
rows = list({r["sku"]: r for r in rows}.values())
if before != len(rows):
    print(f"NOTE: catalog had {before - len(rows)} duplicate SKU(s) — deduped.")
```

### Slow/stuck uploads from Cloud Shell

Cloud Shell's own outbound bandwidth can be surprisingly limited for large
(500MB+) uploads — `gcloud builds submit .` (which tars+uploads the current
directory) can appear to hang for 20+ minutes on a multi-GB source tree.
Check whether it's actually stuck vs. just slow:

```bash
ps aux | grep gcloud                      # still consuming CPU? still alive
gsutil du -sh gs://<bucket>/<expected-object>  # growing over time?
```

If it's genuinely stalled, don't keep re-running the same slow path.

### Rebuilding without re-uploading the full source

Once the source is staged in GCS (step 6), never re-run a local/Cloud-Shell
tar+upload for a small code change again. Point `gcloud builds submit`
directly at the GCS object (positional arg, not `--source=`) with a small
`cloudbuild.yaml` that patches the fetched source in-place before building —
Cloud Build fetches from GCS on Google's own internal network, so this is
fast regardless of Cloud Shell's own bandwidth:

```yaml
steps:
- name: 'busybox'
  entrypoint: 'sh'
  args:
  - -c
  - |
    sed -i 's|OLD_LINE|NEW_LINE|' Dockerfile
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-t', 'IMAGE_TAG', '.']
images:
- 'IMAGE_TAG'
```

```bash
gcloud builds submit gs://BUCKET/staples-deploy-source.tar.gz --config=cloudbuild-patch.yaml
```

For a multi-line code change too large/fragile for `sed`, upload the fixed
file(s) to GCS separately (they're small — seconds, not minutes) and add a
`gcr.io/cloud-builders/gsutil` step per file to copy it into place before
the docker build step, instead of embedding the whole file inline in the
YAML — Cloud Build steps have a **10,000-character argument limit**, which a
full source file easily exceeds:

```yaml
steps:
- name: 'gcr.io/cloud-builders/gsutil'
  args: ['cp', 'gs://BUCKET/patches/main.py', 'backend/main.py']
- name: 'gcr.io/cloud-builders/gsutil'
  args: ['cp', 'gs://BUCKET/patches/products_repo_sql.py', 'backend/products_repo_sql.py']
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-t', 'IMAGE_TAG', '.']
images:
- 'IMAGE_TAG'
```

### `--source` positional argument, not a flag

`gcloud builds submit --source=gs://...` errors with `unrecognized
arguments` on current `gcloud` versions — `SOURCE` is a positional argument:
`gcloud builds submit gs://bucket/object.tar.gz --config=...`.

### `~` doesn't expand after `--flag=`

`--config=~/cloudbuild.yaml` is passed to `gcloud` literally (bash only
tilde-expands at the start of a word, not after `=` inside one). Use
`--config=$HOME/cloudbuild.yaml` instead.

### CDN serves a shorter cache TTL than the objects were uploaded with

Product images were uploaded to GCS with `Cache-Control: public,
max-age=604800` (step 13), but `curl -sI` against the live CDN host showed
`max-age=3600` instead. Root cause: `backend-buckets create --enable-cdn`
was run with no explicit TTL flags, so Cloud CDN falls back to its own
default `client-ttl` (3600s) — which caps what Cloud CDN actually
*advertises* to browsers, independent of the `Cache-Control` header already
sitting on the GCS objects. Fix:

```bash
gcloud compute backend-buckets update staples-images-backend \
  --cache-mode=CACHE_ALL_STATIC \
  --client-ttl=604800 \
  --default-ttl=604800 \
  --max-ttl=604800

# Without this, already-cached objects keep serving the old TTL until they
# naturally expire — the flag change alone doesn't touch what's already cached.
gcloud compute url-maps invalidate-cdn-cache staples-images-lb --path "/products/*"
```

### Hero sample photos silently do nothing (works locally, fails once `IMAGES_BASE_URL` is set)

Symptom: clicking a "No photo handy?" sample thumbnail does nothing — no
error, no navigation. Shop the Room's samples are unaffected. Root cause:
the hero-sample click handler `fetch()`es the same-origin
`/images/products/{sku}.jpg` to get the image bytes before running a
search. Once `IMAGES_BASE_URL` is set, that path 302s to the CDN — a
*different origin* — and `fetch()` enforces CORS on the final response even
across a same-origin-initiated redirect. GCS objects don't send
`Access-Control-Allow-Origin` by default, so the fetch throws with no
visible symptom (an `<img>` tag loading the same URL works fine regardless,
since plain image loads don't enforce CORS — that's why this is easy to
miss in a quick visual check). Fix:

```bash
cat > /tmp/cors.json <<'EOF'
[
  {
    "origin": ["*"],
    "method": ["GET", "HEAD"],
    "responseHeader": ["Content-Type"],
    "maxAgeSeconds": 3600
  }
]
EOF

gsutil cors set /tmp/cors.json gs://${IMAGES_BUCKET}
gsutil cors get gs://${IMAGES_BUCKET}   # verify it took

# Same caveat as the TTL fix above -- purge whatever the CDN already
# cached from before CORS was configured, or the fix won't be visible
# until those entries naturally expire.
gcloud compute url-maps invalidate-cdn-cache staples-images-lb --path "/products/*"

# Confirm the CDN itself (not just GCS) is actually returning the header:
curl -sI -H "Origin: https://<your-cloud-run-url>" \
  https://$CDN_HOST/products/<some-sku>.jpg | grep -i access-control
```

The app-side code was also hardened regardless of this fix (both the hero
and Shop the Room sample handlers now catch and surface fetch failures
instead of failing silently) — see `app.js`'s `renderHeroSamples` /
`renderRoomSamples`.
