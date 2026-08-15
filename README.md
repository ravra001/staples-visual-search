# Staples Visual Search — Hackathon Prototype

A self-contained demo of a "search by photo" feature bolted onto a Staples.com-style
storefront. Upload a photo (chair, cable, ink cartridge, monitor, etc.) via the camera
icon in the search bar, and it returns visually similar products from the catalog.

Everything runs locally with **zero cloud dependencies** for now — this is the
fast-iteration phase. See "Moving to GCP" below for the swap-in points that turn this
into the real Vertex AI-backed architecture from the project's GCP hackathon plan.

## Run it

With **uv** (recommended):
```bash
uv sync                       # or: uv sync --extra clip   (for CLIP mode)
uv run python backend/run.py
```

With **pip**:
```bash
pip install -r requirements.txt
cd backend && python run.py
```

Then open http://localhost:8000 in a browser. All settings live in
**`backend/config.yaml`** (see below) — no environment variables required.
For the full 10k-catalog / offline-CLIP walkthrough, see **`RUN_10K.md`**.

(Images are pre-generated and already committed under
`backend/static/images/products/`. If you ever need to regenerate them —
e.g. after editing `products_data.py` — run `python3 generate_images.py`
from the `backend/` directory.)

## What's here

```
backend/
  main.py              FastAPI app: serves the frontend + JSON API
  products_data.py     Catalog repository (30 products / 9 categories). Pluggable:
                       in-memory (default) OR Cloud SQL, via DATA_BACKEND.
  products_repo_sql.py Cloud SQL (Postgres) implementation of the repository.
  embeddings.py        Pluggable image embedding: heuristic (default) / local CLIP /
                       Vertex AI, via EMBEDDING_BACKEND. + cosine similarity.
  generate_images.py   One-time script that drew the placeholder product photos
  static/images/products/   Generated product images (PNG)
  ingest_abo.py        Builds a ~10k real-name / real-image catalog from the open
                       Amazon Berkeley Objects dataset (demo/non-commercial).
  build_index.py       Precomputes + caches the embedding index (fast startup at scale).
  test_gcp.py          Smoke-tests the Vertex AI + Cloud SQL backends once you auth.
frontend/
  index.html           Homepage (hero, featured products, category grid, rails)
  category.html        Listing page — powers both category browsing AND text search
  product.html         Product detail page (PDP) with "you may also like"
  architecture.html    Visual architecture (on-prem + GCP) — open via the ☰ menu
  visual-search.html   Visual search results page
  assets/style.css     Staples-style visual language (exact brand red #CC0000)
  assets/app.js        All client-side logic — no build step, no framework
requirements.txt       Base deps (heuristic + in-memory — runs with zero extras)
requirements-ml.txt    Optional deps for the CLIP / Vertex / Cloud SQL backends
```

## Big catalog: 10k real products (Amazon Berkeley Objects)

`ingest_abo.py` builds a ~10,000-product catalog with **real product names and
real photos** from the open [Amazon Berkeley Objects](https://amazon-berkeley-objects.s3.amazonaws.com/)
dataset (furniture, seating, tables, lighting, rugs, décor, office & janitorial
supplies, kitchen). **Demo/hackathon use only — ABO images are CC BY-NC 4.0 (non-commercial).**

```bash
# 1. metadata is tiny (~90MB); download listings_*.json.gz + images.csv.gz into data/abo/
# 2. build the catalog json, then pull the 10k images (~200MB, only what's selected):
cd backend
python ingest_abo.py build --abo-dir data/abo
python ingest_abo.py images --workers 40

# 3. precompute the embedding index so startup is instant, then run against it:
CATALOG_FILE=data/catalog_abo.json python build_index.py            # heuristic (~20s)
CATALOG_FILE=data/catalog_abo.json python -m uvicorn main:app --port 8000

# For real-world CLIP matching over the 10k set (slower, one-time):
EMBEDDING_BACKEND=clip CATALOG_FILE=data/catalog_abo.json python build_index.py
EMBEDDING_BACKEND=clip CATALOG_FILE=data/catalog_abo.json python -m uvicorn main:app
```

The frontend categories, nav, and grid are driven by `/api/categories`, so they
adapt to whichever catalog is loaded (the 30-item demo or the 10k set) with no
code change.

## Testing the GCP backends

`gcloud` is not required to run the app, only to exercise the `vertex` / `sql`
backends. **You** authenticate (Claude/automation can't log in for you); then
`test_gcp.py` verifies the wiring:

```bash
gcloud auth application-default login
gcloud config set project <PROJECT_ID>
gcloud services enable aiplatform.googleapis.com sqladmin.googleapis.com
pip install -r requirements-ml.txt

GCP_PROJECT=<id> python test_gcp.py                    # Vertex AI embedding check
GCP_PROJECT=<id> DATABASE_URL=... python test_gcp.py   # + Cloud SQL seed/query check
```

## Backends (configured in `config.yaml`)

The two pieces most likely to change as the project matures — the embedding
model and the catalog store — are each pluggable, and set in **`backend/config.yaml`**.
The defaults need **no** extra dependencies and run fully offline.

| Setting (config.yaml) | Code fallback | Other options |
| --- | --- | --- |
| `embedding.backend` | `heuristic` | `clip` (local OpenCLIP), `vertex` (Vertex AI) |
| `data.backend` | `memory` | `sql` (Cloud SQL / Postgres or SQLite via `data.database_url`) |

> Note: the **shipped `config.yaml` runs `clip` + the 10k catalog** (the demo you'd
> show). `heuristic` / `memory` are the zero-config *code* fallbacks used when no
> config file is present. The embedding index is fingerprinted with the model that
> built it, so changing `clip.model`/`pretrained` without rebuilding is detected and
> the stale cache is refused (no silent mis-ranking).

```yaml
# Real learned embeddings, still 100% offline (needs the clip extra):
embedding:
  backend: clip

# Production target — Vertex AI embeddings + Cloud SQL:
embedding:
  backend: vertex
  vertex: { project: my-proj }
data:
  backend: sql
  database_url: postgresql+psycopg://user:pass@host/staples
```

Then `uv run python backend/run.py`. Every setting also accepts an environment
variable of the matching name (`EMBEDDING_BACKEND`, `DATA_BACKEND`,
`DATABASE_URL`, …) which overrides the file — handy for CI/ops. `GET /api/config`
reports which backends are live. Switching backends changes nothing else —
`main.py`, the API surface, and the whole frontend are unchanged.

## How visual search works right now

1. User clicks the camera icon in the header search bar (or the hero "Try Visual
   Search" button) and picks a photo.
2. The browser POSTs the image to `POST /api/visual-search`.
3. The backend computes a feature vector for the uploaded image using the active
   `EMBEDDING_BACKEND` (see `embeddings.py`). Every catalog image was embedded the
   same way at server startup and cached in memory.
4. Brute-force cosine similarity ranks the catalog against the query vector; top 8
   matches are returned with a `match_score`.
5. Results render on `visual-search.html` using the same product tile component as
   the rest of the site.

### Soft category classifier (coarse-to-fine)

`/api/visual-search` also classifies the uploaded photo into a catalog category
using a **nearest-centroid classifier** — each category's centroid is the mean of
its cached product vectors, so it needs no training and reuses the embeddings we
already have. When the top category is **confident** (≥ `CONF_THRESHOLD`), the
search is *scoped* to that category (fewer vectors, no cross-category noise);
when it's unsure, it falls back to searching the whole catalog. It's a **soft**
filter, never a hard gate — the results page shows a chip ("Detected: Chairs · 61%
— See all categories" / "Looks like Tables — Show Tables only") so a wrong guess
is always one click to override. Response fields: `predicted_category`,
`confidence`, `category_ranking`, `scoped`, `searched`.

**On the default (`heuristic`) backend:** the embedding is a color + shape + edge
histogram — no ML model, runs anywhere. It reliably finds near-duplicates and
separates shape-distinct categories (chair vs. cable), but it will NOT generalize to
arbitrary real-world photos (odd angles, lighting, clutter). **For real-world photo
search, switch to `EMBEDDING_BACKEND=clip` (local, offline) or `vertex`** — the
interface is identical, so nothing downstream changes. This is also why real product
photography only pays off once a learned backend is active: the heuristic is tuned to
clean studio shapes.

## Moving to GCP (per the architecture doc)

Both swap points are **implemented as selectable backends** (see the Backends
table above) — moving to GCP is a matter of flipping config, not rewriting code:

- **Cloud SQL + pgvector** — `DATA_BACKEND=sql` + `DATABASE_URL`. The Postgres
  repository (`products_repo_sql.py`, SQLAlchemy + `pgvector-python`) stores BOTH
  the catalog rows and their vectors — a `products` table with `embedding` (fused,
  HNSW-indexed, used for ranking) and `image_embedding` (pure-image, used for the
  same dual-score display the memory backend does) columns. Vector search runs
  *inside Postgres* via pgvector's `<=>` cosine-distance operator
  (`search_by_vector()`) — `main.py` routes there instead of the in-memory matrix
  whenever this backend is active; the in-memory index isn't built at all.

  Try it locally first (no Cloud SQL needed — `docker-compose.yml` ships a local
  pgvector instance):
  ```bash
  docker compose up -d
  pip install -r requirements-ml.txt   # adds pgvector-python
  cd backend
  DATABASE_URL=postgresql+psycopg://staples:staples@localhost:5432/staples \
    python -c "import products_repo_sql as r; r.init_and_seed(reuse_vectors_from='data/index_clip.npz')"
  ```
  `reuse_vectors_from` reuses the vectors your in-memory index already computed —
  seeding 10k products takes well under a second instead of re-embedding. Then set
  `data.backend: sql` (config.yaml) or `DATA_BACKEND=sql` and run the app normally.

- **Vertex AI Multimodal Embeddings** — `EMBEDDING_BACKEND=vertex` + `GCP_PROJECT`.
  Returns a 1408-dim vector; the pgvector schema's vector width is derived from
  `embeddings.embedding_dim()`, so it adapts automatically. For a 10k–50k catalog,
  brute-force pgvector (or even the in-memory matrix) is still the right call — no
  need for Vertex AI Vector Search / Matching Engine until that stops being instant.

`main.py`'s product-lookup endpoints, all of `frontend/`, and the whole upload UX
carry over unchanged regardless of which backend is active.

## Supplying real product images

Product photos are generated placeholders. To use real photography, drop a
`<SKU>.png` file per product into `backend/static/images/products/` (same filename as
the SKU) — the app serves and embeds them automatically, no code change. Real photos
are worth adding **once `EMBEDDING_BACKEND` is `clip` or `vertex`**; on the heuristic
backend they can hurt match quality.

## Notes

- Not affiliated with Staples, Inc. — this is a demo storefront skin, not real Staples
  data. The 30-item quick-start catalog is fictional placeholder data. The 10k catalog
  (`ingest_abo.py`, see above) is **real product names/photography from the open Amazon
  Berkeley Objects dataset, licensed CC BY-NC 4.0 (non-commercial)** — demo/hackathon use
  only, not a license to ship in any commercial product.
- Cart persists client-side in `localStorage` (a counter — no checkout).
- Text search is wired to `GET /api/search` (name/brand/category/description match).
  Visual search remains the headline feature.
