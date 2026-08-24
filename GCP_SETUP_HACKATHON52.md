# GCP onboarding — prj-spls-np-hackathon52-000

Updated after actually trying this against the real project: your account
(via the `gcp-sds-hackathon@staples.com` group) has admin rights on the
*AI/data* APIs (Vertex AI, Vision AI, Secret Manager, Storage, Dataform)
but **no permission to create service accounts, deploy Cloud Run, or
provision Cloud SQL/Artifact Registry**. The hackathon's own onboarding
doc confirms the intended pattern is different from a from-scratch Cloud
Run deployment: call Vertex AI directly, authenticated either as yourself
or via a pre-existing service account's downloaded JSON key
(`sa-np-hackathon52-000@prj-spls-np-hackathon52-000.iam.gserviceaccount.com`).
Also: **Vertex AI/Gemini for this project is in `us-east4`**, not
`us-central1`.

This file is split into two parts:

- **Part A** works right now, with the permissions you already have — get
  Staples AI (Gemini) and voice input running **locally** against this
  project.
- **Part B** is the full Cloud Run + Cloud SQL deployment from the
  original plan — **on hold** until you confirm with the project admin
  (`adminleige001@staples.com` holds `roles/owner`) whether that's even
  the intended path for this hackathon, and if so, get the missing roles
  granted (`run.admin`, a Cloud SQL admin role, an Artifact Registry admin
  role, and either `iam.serviceAccountAdmin` or explicit permission to act
  as the existing `sa-np-hackathon52-000` service account).

---

## Part A — Run it locally against this project, right now

No blocked permissions needed for any of this.

### A1. Get the service account key

Per the hackathon's own instructions: Console → **IAM & Admin → Service
Accounts** → find
`sa-np-hackathon52-000@prj-spls-np-hackathon52-000.iam.gserviceaccount.com`
→ **Keys** tab → **Add key** → **Create new key** → JSON. This downloads a
`.json` file — treat it like a password (it's a real credential for this
project's service account); don't commit it to the repo or share it.

### A2. Point the app at it

From the repo's `backend/` directory:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/the-downloaded-key.json"
export GCP_PROJECT="prj-spls-np-hackathon52-000"
export GCP_LOCATION="us-east4"
```

`GOOGLE_APPLICATION_CREDENTIALS` is the standard variable every Google
client library checks automatically (`google-cloud-aiplatform`,
`google-cloud-speech` — both already used in this app) — this replaces
the interactive `gcloud auth application-default login` step the app's
other docs (`RUN_10K.md`) describe; no code changes needed either way.

### A3. Enable the APIs this project needs (if not already)

You likely already have `serviceusage.serviceUsageAdmin` via the group
(this worked earlier), so this should succeed:

```bash
gcloud config set project prj-spls-np-hackathon52-000
gcloud services enable aiplatform.googleapis.com speech.googleapis.com
```

If `speech.googleapis.com` fails to enable or the service account lacks
the `roles/speech.editor` role once you test voice, that's a role binding
someone with IAM admin on this project (the group, or the owner) needs to
grant to `sa-np-hackathon52-000` — you likely can't self-grant it (IAM
policy changes weren't in the group's role list either).

### A4. Run the app

```bash
cd backend
uv run python run.py
```

(or `python run.py` in an activated venv — see `RUN_10K.md` for the full
local-run instructions if this is a fresh checkout). Open
`http://localhost:8000`, open Staples AI, and confirm it gives a real
Gemini reply instead of a degraded/generic one. Same for the mic — that
confirms both `aiplatform.user` and (once granted) `speech.editor` are
working for `sa-np-hackathon52-000`.

**Test the Vertex AI connection directly first**, if you want to isolate
"is auth actually working" from "is the app wired up right":

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://aiplatform.googleapis.com/v1/projects/prj-spls-np-hackathon52-000/locations/us-east4/publishers/google/models/gemini-2.5-flash:generateContent" \
  -d '{"contents": [{"role": "user", "parts": [{"text": "tell me a joke"}]}], "generationConfig": {"responseModalities": ["TEXT"], "temperature": 0.2, "maxOutputTokens": 1024, "topP": 0.8}}'
```

(This uses your own user credentials via `gcloud auth print-access-token`,
not the service account key — the hackathon doc's example, useful as a
sanity check independent of the JSON key working.)

---

## Part B — Full Cloud Run deployment (ON HOLD — confirm with admin first)

Everything below is the original from-scratch plan. **Do not start this
until you've confirmed two things with whoever administers this
project**:

1. Is Cloud Run + Cloud SQL actually the intended deployment path for
   this hackathon, or is there different infrastructure already set up
   that you should be pointed at instead?
2. If yes, request these roles (either for your own account, or
   confirmation that `sa-np-hackathon52-000` will be used and who can
   bind roles to it):
   - `roles/run.admin` (deploy/manage Cloud Run services)
   - `roles/cloudsql.admin` (create the Postgres instance)
   - `roles/artifactregistry.admin` (create the image repo)
   - `roles/iam.serviceAccountUser` on `sa-np-hackathon52-000` specifically
     (needed to deploy Cloud Run *as* that account, without needing to
     create a new one) — this is likely the smoother ask than requesting
     `iam.serviceAccountAdmin` outright, since it doesn't grant broad
     account-creation rights, just "let me deploy things that run as this
     one already-existing account."

Once that's confirmed, here's the plan, using the **existing**
`sa-np-hackathon52-000` service account (not creating a new one) and the
corrected `us-east4` region:

### B0. One-time setup

```bash
export PROJECT_ID="prj-spls-np-hackathon52-000"
export REGION="us-east4"
export SQL_INSTANCE="staples-pgvector"
export DB_NAME="staples"
export DB_USER="staples_app"
export SERVICE_NAME="staples-visual-search"
export AR_REPO="staples-repo"
export SA_EMAIL="sa-np-hackathon52-000@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
```

### B1. Confirm the extra roles landed

```bash
gcloud projects get-iam-policy "$PROJECT_ID" > /tmp/iam-policy.yaml
cat /tmp/iam-policy.yaml
```

Look for `roles/run.admin`, a Cloud SQL role, and an Artifact Registry
role bound to either your account/group or `sa-np-hackathon52-000`,
depending on what was actually granted.

### B2. Enable remaining APIs

```bash
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com
```

(`aiplatform` and `speech` already enabled in Part A.)

### B3. Bind the remaining roles to the existing service account

Only run this if you were told YOU have permission to bind roles (not
just enable APIs) — otherwise ask the admin to run this instead:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/cloudsql.client"
```

(`secretmanager.secretAccessor` and `aiplatform.user` are already bound
to this account per the policy dump we already pulled.)

### B4. Provision Cloud SQL for Postgres (~10 min, real cost starts here)

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

For seeding (step B7), direct access:

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

Cloud Shell's public IP is ephemeral — if a later step suddenly times out
after working before, re-run the `MY_IP`/`authorized-networks` lines
(check what's already authorized first with `gcloud sql instances
describe "$SQL_INSTANCE" --format="value(settings.ipConfiguration.authorizedNetworks)"`
since `--authorized-networks` REPLACES the whole list).

### B5. Artifact Registry

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Staples Visual Search images"
```

### B6. Get the code into Cloud Shell and stage it in GCS

```bash
mkdir -p ~/staples-deploy && cd ~/staples-deploy
git clone https://github.com/ravra001/staples-visual-search.git .

gsutil mb -l "$REGION" "gs://${PROJECT_ID}-deploy-src" 2>/dev/null || true
tar -czf /tmp/source.tar.gz --exclude='.git' .
gsutil cp /tmp/source.tar.gz "gs://${PROJECT_ID}-deploy-src/staples-deploy-source.tar.gz"
```

**The CLIP model (`backend/models/hf/`) needs staging separately** — it's
`.gitignore`'d (exceeds GitHub's 100MB limit). Zip it from a machine that
has it populated and stage it:

```bash
# gsutil cp hf_model.zip gs://${PROJECT_ID}-deploy-src/model-cache/hf_model.zip
gsutil ls -lh "gs://${PROJECT_ID}-deploy-src/model-cache/hf_model.zip"
```

### B7. Seed the database

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

### B8. Build and push the image

```bash
cd ~/staples-deploy
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"

gcloud builds submit "gs://${PROJECT_ID}-deploy-src/staples-deploy-source.tar.gz" \
  --tag "$IMAGE"
```

### B9. Store the DB connection as a secret

```bash
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=staples-db-password)
printf 'postgresql+psycopg://%s:%s@/%s?host=/cloudsql/%s' \
  "$DB_USER" "$DB_PASSWORD" "$DB_NAME" "$SQL_CONNECTION_NAME" | \
  gcloud secrets create staples-database-url --data-file=-

gcloud secrets add-iam-policy-binding staples-database-url \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

### B10. Deploy to Cloud Run

```bash
gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --add-cloudsql-instances "$SQL_CONNECTION_NAME" \
  --set-secrets "DATABASE_URL=staples-database-url:latest" \
  --set-env-vars "GCP_PROJECT=${PROJECT_ID},GCP_LOCATION=us-east4,EMBEDDING_BACKEND=clip,DATA_BACKEND=sql" \
  --min-instances=1 \
  --max-instances=10 \
  --memory=4Gi \
  --cpu=2 \
  --concurrency=20 \
  --timeout=300 \
  --allow-unauthenticated

gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)'
```

Note `GCP_LOCATION=us-east4` explicitly set — this project's Vertex AI
access is in that region, not the `us-central1` default the app otherwise
assumes.

### B11. Verify

```bash
export URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)')
curl -s "$URL/api/config"
curl -s "$URL/api/categories"
curl -s -o /dev/null -w "products: %{time_total}s\n" "$URL/api/products"
```

### B12. Continuous deployment via GitHub

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

Console → Cloud Build → Triggers → Connect Repository → GitHub → select
`staples-visual-search`. Create trigger: push to `^main$`, configuration =
**"Cloud Build configuration file"**, location `/cloudbuild.yaml`.

⚠️ **Do NOT pick "Autodetected"/"Dockerfile"** — it bypasses
`cloudbuild.yaml` and deploys a second, broken service with the wrong
region/service-account/config. Confirm with
`gcloud builds triggers list --format="table(name,filename)"` — an empty
`filename` column means it's on the wrong path.

Set `cloudbuild.yaml`'s `_MODEL_BUCKET` substitution (Cloud Build console
→ trigger → Settings) to wherever you staged the zipped model in B6, and
add `GCP_LOCATION=us-east4` to the `--set-env-vars` line in
`cloudbuild.yaml`'s deploy step (it currently only sets `GCP_PROJECT`).

### B13. Product images via GCS + Cloud CDN (optional)

Same as the general `GCP_SETUP.md` step 13 — not reproduced here since
it's independent of the region/service-account issues above. See that
file's step 13 when you get to it.

---

## Quick reference: what went wrong last time (so you recognize it faster)

- **`Permission iam.serviceAccounts.create denied`** — your account has no
  IAM-admin role on this project; the fix isn't a gcloud flag, it's asking
  the project owner to grant a role (or use the existing service account
  instead, per Part A/B above).
- **Generic `503` / "service not available, try again in 30 seconds"** —
  on the previous project, this traced back to a billing hold silently
  disabling APIs (including Cloud Build itself), independent of any app
  bug. Check `gcloud billing projects describe "$PROJECT_ID"` and
  `gcloud services list --enabled` first if this recurs.
- **A GitHub push doesn't trigger a build** — `gcloud builds triggers
  list` to confirm the trigger exists/isn't disabled;
  `gcloud builds list --limit=5` to see if anything attempted to run.
- **Voice input "unavailable"** — check Cloud Run's logs for the real
  traceback (the browser only ever sees a generic message by design):
  `gcloud logging read 'resource.type=cloud_run_revision AND
  resource.labels.service_name=staples-visual-search AND
  textPayload:"speech"' --limit=20 --freshness=1h
  --format='value(textPayload)'`
- **Everything else** — `GCP_SETUP.md`'s full Troubleshooting section.
