# Staples Visual Search — Hackathon Prototype

A Staples.com-style storefront with two search/shopping surfaces bolted on,
backed by a real ~10,000-product catalog (Amazon Berkeley Objects, restyled)
and real CLIP embeddings:

- **Search by photo** — upload a photo of a chair, lamp, rug, or other
  home/office item via the camera icon in the search bar, and it ranks the
  catalog by visual similarity. Text-refined queries ("but in black"),
  crop-to-search, "find similar" from any product, "Complete the Look"
  cross-sell, and **Shop the Room** — one photo of a whole room (or a supply
  shelf, for a B2B reorder framing) returns one best-matching product per
  distinct item detected, not a pile of near-duplicates.
- **Staples AI** — a Gemini (Vertex AI) tool-calling chat, in-process in the
  same FastAPI service. Eleven tools, every one a thin wrapper over
  deterministic Python/SQL that already exists elsewhere in this app (see
  `backend/agent.py`'s module docstring for the full list) — plan an office
  or room setup within budget, find deals, compare products, swap a bundle
  item for something cheaper/nicer, or reorder from a photographed receipt
  or shopping list. Falls back to plain hybrid search (`degraded: true`)
  if Vertex isn't configured, rather than pretending to be smarter than it is.

Also: hybrid keyword+semantic text search (the header search bar), and an
honest "no strong matches" state when a photo genuinely isn't in the catalog.

This runs two ways: **fully offline** (in-memory catalog, bundled CLIP model,
zero cloud calls — the fast-iteration/local-dev path), or **deployed on GCP**
(Cloud Run + Cloud SQL/pgvector, `DATA_BACKEND=sql`) — which is how it's
actually running in production for this project. See **`GCP_SETUP.md`** for
the full deployment runbook (project → IAM → Cloud SQL/pgvector → Cloud Run,
with every real bug hit along the way and how it was fixed), and "Moving to
GCP" below for the backend swap-in points.

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
  main.py              FastAPI app: all HTTP endpoints, the visual-search/
                       Shop the Room/hybrid-search pipeline, and _do_agent_chat
                       (Staples AI's orchestration loop over agent.py's tools).
  agent.py             Staples AI: Gemini tool declarations + the deterministic
                       catalog-math tools it calls (find_deals, compare_products,
                       plan_office_setup, swap_bundle_item, ...). See its module
                       docstring for the full eleven-tool list.
  config.py / config.yaml   ALL configuration lives here (no scattered env vars).
  products_data.py     Catalog repository. Pluggable: in-memory (default, loads
                       the 30-item built-in demo OR the 10k ABO set via
                       CATALOG_FILE) OR Cloud SQL, via DATA_BACKEND.
  products_repo_sql.py Cloud SQL (Postgres + pgvector) implementation — what's
                       actually deployed in production (DATA_BACKEND=sql).
  embeddings.py        Pluggable image embedding: heuristic (default) / local CLIP /
                       Vertex AI, via EMBEDDING_BACKEND.
  text_match.py         Keyword-side matching for hybrid text search (fused with
                       CLIP-text semantic ranking via Reciprocal Rank Fusion).
  generate_images.py   One-time script that drew the placeholder product photos
  static/images/products/   Generated product images (PNG) + the 10k ABO photos
  ingest_abo.py        Builds a ~10k real-name / real-image catalog from the open
                       Amazon Berkeley Objects dataset (demo/non-commercial).
  build_staples_catalog.py, fix_catalog_categories.py,
  apply_category_corrections.py   One-off catalog-cleanup scripts run once
                       against catalog_abo.json, not part of the live app.
  build_index.py       Precomputes + caches the embedding index (fast startup at scale).
  test_gcp.py          Smoke-tests the Vertex AI + Cloud SQL backends once you auth.
frontend/
  index.html           Homepage — two-column hero (Visual Search / Staples AI,
                       each with click-to-try examples), featured products,
                       category grid, Deals/Popular rails.
  category.html        Listing page — powers both category browsing AND text search
  product.html         Product detail page (PDP) with Visually Similar / Complete
                       the Look / Similar for Less rails
  visual-search.html   Visual search results page
  shop-the-room.html   Shop the Room results page (one match per detected item)
  cart.html            Real cart page (line items, quantities — not just a counter)
  how-it-works.html    The current, detailed architecture walkthrough — prefer
                       this over HOW_IT_WORKS.md, which predates most of this list.
  architecture.html    Visual architecture diagram (on-prem + GCP) — ☰ menu
  pitch.html           Demo/pitch page
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
# What's actually deployed (Cloud Run): local CLIP + Cloud SQL/pgvector.
# CLIP was benchmarked against Vertex AI Multimodal Embeddings and kept —
# see how-it-works.html's "Where this maps to GCP" section for the numbers
# (CLIP ties/leads recall@5+ at $0 marginal cost vs. Vertex's better
# recall@1 at ~$0.0001/image and 3-4x the latency).
embedding:
  backend: clip
data:
  backend: sql
  database_url: postgresql+psycopg://user:pass@host/staples

# Vertex AI embeddings are also wired up and switchable (needs GCP_PROJECT):
embedding:
  backend: vertex
  vertex: { project: my-proj }
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
   `EMBEDDING_BACKEND` (see `embeddings.py`).
4. The catalog side depends on `DATA_BACKEND`: **memory** embeds every catalog
   image once at startup and ranks with a brute-force NumPy matmul; **sql**
   (what's deployed) never loads catalog vectors into the process at all — every
   query is one pgvector HNSW-indexed similarity query straight against Postgres.
   Either way, up to 48 matches are returned with a `match_score` (`search.top_k`
   in `config.yaml`).
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
- Cart is client-side (`localStorage`), but a real one — line items, quantities,
  a running total on `cart.html` — not just a header counter. No checkout/payment.
- Text search (the header search bar) is hybrid: keyword matching fused with
  CLIP-text-vs-catalog-vector semantic ranking via Reciprocal Rank Fusion, plus
  price-intent parsing ("under $50", "cheaper") handled client-side as a
  sort/filter rather than sent through the embedding model. Visual search
  remains the headline feature, but text search and Staples AI are no longer
  a thin afterthought next to it.
