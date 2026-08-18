# Staples Visual Search — production image for Cloud Run.
#
# Bundles the CLIP model (~577MB, offline-first — no HuggingFace download at
# cold start), the 10k product catalog + images, and the prebuilt embedding
# index, so a fresh container serves traffic immediately with no warm-up
# embedding pass. This matches the project's "never re-embed at runtime"
# design (see backend/main.py's REQUIRE_PREBUILT_INDEX check).
#
# Build (from the repo root):
#   gcloud builds submit --tag <region>-docker.pkg.dev/<project>/<repo>/staples-visual-search
# (Cloud Build — no local Docker needed. See GCP_SETUP.md for the full flow.)

FROM python:3.13-slim

WORKDIR /app

# System deps: psycopg[binary] ships its own libpq, so no build-essential/
# libpq-dev needed. curl is handy for the container's own healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (separate layer — cached across code-only changes).
COPY requirements.txt requirements-ml.txt ./
# torch and torchvision MUST come from the same CPU-only index in the same
# install invocation — pulling torchvision afterward from default PyPI (or in
# a separate pip call) resolves a CUDA build that doesn't match this torch
# build, and CLIP preprocessing fails at runtime with "operator
# torchvision::nms does not exist".
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-ml.txt

# App code + bundled data/model assets. See .dockerignore for what's excluded
# (notably: the experimental/ multi-angle data, .venv, .git — but NOT
# backend/models/hf, which IS gitignored but must still ship in the image).
COPY backend/ ./backend/
COPY frontend/ ./frontend/
# Root docs linked from the architecture/how-it-works pages (main.py's
# root_doc route) — without these the container 500s on a click, not a
# clean 404, since FileResponse fails at send-time on a missing path.
COPY README.md GCP_SETUP.md HOW_IT_WORKS.md RUN_10K.md ./

# Cloud Run injects $PORT and expects the container to listen on 0.0.0.0 —
# config.py already reads PORT from the environment; only HOST needs the
# override (its local default is 127.0.0.1).
ENV HOST=0.0.0.0
ENV EMBEDDING_BACKEND=clip
ENV DATA_BACKEND=sql
ENV CATALOG_FILE=data/catalog_abo.json
# DATABASE_URL is supplied at deploy time via Secret Manager (see GCP_SETUP.md) —
# never baked into the image.

WORKDIR /app/backend
EXPOSE 8080
CMD ["python", "run.py"]
