# GCP onboarding — prj-spls-np-hackathon52-000

A copy-paste, run-in-order runbook for standing this app up on the
hackathon-provided project, from scratch. Every variable below is already
filled in with this project's real values — nothing to substitute. Run
everything in **Cloud Shell** (browser-based, already authenticated) — do
NOT try this from a local machine's `gcloud` (see `GCP_SETUP.md`'s "Why
Cloud Shell, not local gcloud" section for why).

This is a trimmed, pre-filled version of the full `GCP_SETUP.md` runbook,
folding in everything that actually went wrong setting this app up the
first time on a different project — a dedicated service account from the
very start (not the default compute one), every API enabled up front
(including Speech-to-Text and Vertex AI, easy to forget until voice/chat
breaks later), and the two gotchas that cost real debugging time: the
Cloud Build trigger's "Autodetected" trap, and a billing hold silently
disabling APIs (including Cloud Build itself) independent of any app bug.

If you get stuck, `GCP_SETUP.md`'s "Troubleshooting" section has the full
story (with exact error text) for most of what can go wrong here.

---

## 0. One-time setup — open Cloud Shell and paste this whole block

```bash
export PROJECT_ID="prj-spls-np-hackathon52-000"
export REGION="us-central1"
export SQL_INSTANCE="staples-pgvector"
export DB_NAME="staples"
export DB_USER="staples_app"
export SERVICE_NAME="staples-visual-search"
export AR_REPO="staples-repo"
export SA_NAME="staples-run-sa"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
```

**If you open a new Cloud Shell tab later, re-run this export block first**
— everything below assumes these variables are set in your current shell.

---

## 1. Confirm access and billing

```bash
gcloud projects describe "$PROJECT_ID" --format="value(projectId,lifecycleState)"
gcloud billing projects describe "$PROJECT_ID" --format="value(billingAccountName,billingEnabled)"
```

Expect `ACTIVE` and `billingEnabled: True`. If billing isn't enabled, stop
here and get that sorted with whoever provisioned the project — nothing
below will work without it, and a billing problem can produce confusing
`503`/`API not enabled` errors that look like app bugs (this happened once
already — see `GCP_SETUP.md`'s troubleshooting section for what that
looked like).

---

## 2. Enable every API this app needs, up front

```bash
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  compute.googleapis.com \
  aiplatform.googleapis.com \
  speech.googleapis.com
```

`aiplatform` (Staples AI chat / Gemini) and `speech` (voice input) are
included from the start this time — enabling them only when that feature
breaks later is how the last project lost time on this.

Verify:

```bash
gcloud services list --enabled --format="value(name)" | sort
```

---

## 3. Dedicated service account for Cloud Run (not the default compute one)

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

# Vertex AI (Staples AI chat / Gemini)
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user"

# Cloud Speech-to-Text (voice input) -- roles/speech.editor is the
# predefined role; if this errors, the exact error names the correct one.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/speech.editor"
```

Doing all four IAM bindings now (not just Cloud SQL) means Cloud Run is
deployed with the RIGHT service account from step 10 onward — no need to
go back and re-bind roles after the fact like last time.

---

## 4. Provision Cloud SQL for Postgres (~10 min, real cost starts here)

```bash
gcloud sql instances create "$SQL_INSTANCE" \
  --database-version=POSTGRES_16 \
  --region="$REGION" \
  --tier=db-custom-2-8192 \
  --storage-size=20 \
  --storage-auto-increase \
  --availability-type=zonal

gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"

openssl rand -base64 24 | tr -d '\n' | \
  gcloud secrets create staples-db-password --data-file=-

gcloud sql users create "$DB_USER" \
  --instance="$SQL_INSTANCE" \
  --password="$(gcloud secrets versions access latest --secret=staples-db-password)"

gcloud secrets add-iam-policy-binding staples-db-password \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

For seeding the database in step 7 below, you'll need direct access
(public IP + your Cloud Shell IP authorized):

```bash
gcloud sql instances patch "$SQL_INSTANCE" --assign-ip

export MY_IP=$(curl -s ifconfig.me)
gcloud sql instances patch "$SQL_INSTANCE" \
  --authorized-networks="${MY_IP}/32"

export SQL_CONNECTION_NAME=$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')
export SQL_PUBLIC_IP=$(gcloud sql instances describe "$SQL_INSTANCE" --format='value(ipAddresses[0].ipAddress)')
echo "Connection name: $SQL_CONNECTION_NAME"
echo "Public IP:        $SQL_PUBLIC_IP"
```

**Cloud Shell's public IP is ephemeral** — if a later step suddenly gets a
connection timeout after working fine earlier, re-run the `MY_IP` +
`authorized-networks` lines above (check what's already authorized first
with `gcloud sql instances describe "$SQL_INSTANCE" --format="value(settings.ipConfiguration.authorizedNetworks)"` since `--authorized-networks` REPLACES the whole list, it doesn't append).

---

## 5. Artifact Registry — where the built image lives

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Staples Visual Search images"
```

---

## 6. Get the code into Cloud Shell and stage it in GCS

```bash
mkdir -p ~/staples-deploy && cd ~/staples-deploy
git clone https://github.com/ravra001/staples-visual-search.git .
```

If the repo is private, either make it public (Settings → Danger Zone —
fine for a hackathon repo) or clone with a token:
`git clone https://<token>@github.com/ravra001/staples-visual-search.git .`

Stage the source in GCS once — every later build references this instead
of re-uploading:

```bash
gsutil mb -l "$REGION" "gs://${PROJECT_ID}-deploy-src" 2>/dev/null || true
tar -czf /tmp/source.tar.gz --exclude='.git' .
gsutil cp /tmp/source.tar.gz "gs://${PROJECT_ID}-deploy-src/staples-deploy-source.tar.gz"
```

Budget several minutes for this first upload (the bundled CLIP model +
product images make the source tree large). If it seems stuck, check
`ps aux | grep gcloud` (still consuming CPU?) and
`gsutil du -sh gs://${PROJECT_ID}-deploy-src/...` (still growing?) before
assuming it's actually hung.

**The CLIP model needs staging separately** — `backend/models/` is
`.gitignore`'d (exceeds GitHub's 100MB limit), so a GitHub-checkout-based
build (which is what the Cloud Build trigger in step 12 uses) won't have
it unless it's fetched from GCS during the build. If you have the model
files locally (from a working copy of this project), zip and stage them:

```bash
# From a machine that has backend/models/hf/ populated:
#   cd backend/models && zip -r hf_model.zip hf/
#   gsutil cp hf_model.zip gs://${PROJECT_ID}-deploy-src/model-cache/hf_model.zip
# Then here in Cloud Shell, confirm it's there:
gsutil ls -lh "gs://${PROJECT_ID}-deploy-src/model-cache/hf_model.zip"
```

If that file doesn't exist yet, the manual build in step 8 (which builds
from THIS Cloud Shell checkout, models included if you `git clone`d a repo
that has them, or copied them in manually) works fine to get a first
deploy live — just make sure `backend/models/hf/` actually has content
before building, however you get it there.

---

## 7. Seed the database

```bash
cd ~/staples-deploy/backend
pip install -r ../requirements.txt -r ../requirements-ml.txt

export DATABASE_URL="postgresql+psycopg://${DB_USER}:$(gcloud secrets versions access latest --secret=staples-db-password)@${SQL_PUBLIC_IP}:5432/${DB_NAME}"
export DATA_BACKEND=sql
export CATALOG_FILE=data/catalog_abo.json

python -c "
import products_repo_sql as r
r.init_and_seed(reuse_vectors_from='data/index_clip.npz')
"
```

Sanity-check:

```bash
python -c "
import products_repo_sql as r
print('categories:', r.get_categories())
print('total:', r.count_by_category())
"
```

---

## 8. Build and push the Docker image (first deploy — manual)

```bash
cd ~/staples-deploy
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"

gcloud builds submit "gs://${PROJECT_ID}-deploy-src/staples-deploy-source.tar.gz" \
  --tag "$IMAGE"
```

---

## 9. Store the DB connection as a secret Cloud Run can read

```bash
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=staples-db-password)
printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' \
  "$DB_USER" "$DB_PASSWORD" "$DB_NAME" "$SQL_CONNECTION_NAME" | \
  gcloud secrets create staples-database-url --data-file=-

gcloud secrets add-iam-policy-binding staples-database-url \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 10. Deploy to Cloud Run

```bash
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$SQL_CONNECTION_NAME" \
  --set-secrets "DATABASE_URL=staples-database-url:latest" \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},EMBEDDING_BACKEND=clip,DATA_BACKEND=sql" \
  --min-instances=1 \
  --max-instances=10 \
  --memory=4Gi \
  --cpu=2 \
  --concurrency=20 \
  --timeout=300 \
  --allow-unauthenticated

gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)'
```

`GCP_PROJECT` is set explicitly here (unlike the original project, where
it came from a Cloud Build substitution) since this is a manual first
deploy — the `cloudbuild.yaml` trigger set up in step 12 will keep passing
it on every future auto-deploy too.

---

## 11. Verify

```bash
export URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)')

curl -s "$URL/api/config"
curl -s "$URL/api/categories"
curl -s -o /dev/null -w "products: %{time_total}s\n" "$URL/api/products"
```

All should return in well under a second. If `/api/config` shows
`"data_backend":"memory"` instead of `"sql"`, the `DATA_BACKEND` env var
didn't take — check the deploy command above actually included it.

---

## 12. Continuous deployment via GitHub

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/run.admin"

gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:${CLOUDBUILD_SA}" \
  --role="roles/iam.serviceAccountUser"

gsutil iam ch "serviceAccount:${CLOUDBUILD_SA}:objectViewer" \
  "gs://${PROJECT_ID}-deploy-src"
```

**Connect the repo**: Cloud Console → Cloud Build → Triggers → Connect
Repository → GitHub → authorize → select `staples-visual-search`. This
step is a browser OAuth flow, no CLI path for it.

**Create the trigger**: event = push to branch `^main$`, configuration =
**"Cloud Build configuration file (YAML or JSON)"**, location =
`/cloudbuild.yaml`.

⚠️ **Do NOT pick "Autodetected" / "Dockerfile"** even though the console
may suggest it first — that path ignores this repo's `cloudbuild.yaml`
entirely and deploys through its own wizard instead: wrong region, the
project's default Compute Engine service account (not the one set up in
step 3), and none of the Cloud SQL/secrets/scaling flags from step 10. The
result is a second, broken Cloud Run service that can never reach the
database. Confirm which path a trigger is actually on with:
`gcloud builds triggers list --format="table(name,filename)"` — an empty
`filename` column means it's on the wrong (wizard) path.

You'll also need `cloudbuild.yaml`'s `_MODEL_BUCKET` substitution pointing
at wherever you staged the zipped CLIP model (step 6) — set it in Cloud
Build console → your trigger → Settings → Substitution variables, or edit
the default in `cloudbuild.yaml` directly for this project.

From then on, `git push origin main` is the entire deploy process.

---

## 13. Product images via GCS + Cloud CDN (optional, recommended)

Frees the container's CPU/concurrency budget (shared with CLIP inference)
from serving static image bytes.

```bash
export IMAGES_BUCKET="${PROJECT_ID}-product-images"

gsutil mb -l "$REGION" "gs://${IMAGES_BUCKET}"
gsutil uniformbucketlevelaccess set on "gs://${IMAGES_BUCKET}"
gsutil iam ch allUsers:objectViewer "gs://${IMAGES_BUCKET}"

cd ~/staples-deploy/backend/static/images/products
gsutil -m -h "Cache-Control:public, max-age=604800" cp -r . "gs://${IMAGES_BUCKET}/products/"

gcloud compute backend-buckets create staples-images-backend \
  --gcs-bucket-name="$IMAGES_BUCKET" --enable-cdn

gcloud compute addresses create staples-images-ip --global
export LB_IP=$(gcloud compute addresses describe staples-images-ip --global --format='value(address)')
export CDN_HOST="$(echo $LB_IP | tr '.' '-').sslip.io"

gcloud compute ssl-certificates create staples-images-cert \
  --domains="$CDN_HOST" --global

gcloud compute url-maps create staples-images-lb \
  --default-backend-bucket=staples-images-backend
gcloud compute target-https-proxies create staples-images-https-proxy \
  --url-map=staples-images-lb --ssl-certificates=staples-images-cert
gcloud compute forwarding-rules create staples-images-https-rule \
  --address=staples-images-ip --global \
  --target-https-proxy=staples-images-https-proxy --ports=443
```

**The managed cert takes 15-60 min to provision.** Poll it:

```bash
gcloud compute ssl-certificates describe staples-images-cert \
  --global --format='value(managed.status)'
# PROVISIONING -> ACTIVE
```

Once `ACTIVE`, verify the CDN actually serves an image before cutting
over:

```bash
curl -sI "https://${CDN_HOST}/products/$(gsutil ls gs://${IMAGES_BUCKET}/products/ | head -1 | xargs basename)"
```

Then set it CORS-enabled (needed for the homepage's sample-photo buttons)
and point the service at it:

```bash
cat > /tmp/cors.json <<'CORSEOF'
[{"origin": ["*"], "method": ["GET", "HEAD"], "responseHeader": ["Content-Type"], "maxAgeSeconds": 3600}]
CORSEOF
gsutil cors set /tmp/cors.json "gs://${IMAGES_BUCKET}"

gcloud run services update "$SERVICE_NAME" --region "$REGION" \
  --set-env-vars="IMAGES_BASE_URL=https://${CDN_HOST}"
```

---

## Quick reference: what to do if something breaks

- **Generic `503` / "service not available, try again in 30 seconds"** —
  check billing is actually enabled AND propagated
  (`gcloud billing projects describe "$PROJECT_ID"`), and that no APIs got
  silently disabled (`gcloud services list --enabled` — cross-check
  against the list in step 2). This happened once already and looked
  exactly like an app bug until traced back to billing.
- **`Failed to trigger build: ... requires billing to be enabled`** — same
  cause as above, not a Cloud Build-specific problem.
- **A GitHub push doesn't trigger a build at all** —
  `gcloud builds triggers list` to confirm the trigger exists and isn't
  disabled; `gcloud builds list --limit=5` to see if anything even
  attempted to run.
- **`torchvision::nms does not exist`** — torch/torchvision installed from
  mismatched indexes; see the Dockerfile, they must install together from
  the CPU-only PyTorch index in one command (already correct in this
  repo's `Dockerfile` — only relevant if you've edited it).
- **Voice input "unavailable"** — check Cloud Run's own logs for the real
  traceback (`gcloud logging read 'resource.type=cloud_run_revision AND
  resource.labels.service_name=staples-visual-search AND
  textPayload:"speech"' --limit=20 --freshness=1h
  --format='value(textPayload)'`) — the browser only ever sees a generic
  message by design.
- **Everything else** — `GCP_SETUP.md`'s full Troubleshooting section has
  more scenarios with exact error text from the first time this app was
  deployed.
