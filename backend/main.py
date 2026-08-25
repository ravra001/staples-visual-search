"""
Staples-style visual search demo — FastAPI backend.

Two backends, chosen via config (DATA_BACKEND / EMBEDDING_BACKEND), both
live and in production use:
  - products_data.py -> in-memory catalog (dev/demo) OR products_repo_sql.py
                         -> Cloud SQL (Postgres + pgvector), the one this
                         service actually runs under on Cloud Run.
  - embeddings.py     -> local CLIP (bundled in the container, what's
                         deployed) OR Vertex AI Multimodal Embeddings
                         (benchmarked, not used — see how-it-works.html's
                         "Where this maps to GCP" section for the numbers).

Also home to agent.py's orchestration loop (_do_agent_chat) — the Staples
AI chat's eleven tools are thin wrappers over the same search/CV pipeline
defined below, not a separate implementation.
"""
import asyncio
import base64
import os
import queue as queue_module
import re
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager

import numpy as np
import config
import agent
import speech
from text_match import singularize
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from products_data import (
    DATA_BACKEND,
    get_all_products,
    get_product_by_sku,
    get_products_by_skus,
    get_products_by_category,
    search_products,
    catalog_content_hash,
)
from embeddings import BACKEND as EMBEDDING_BACKEND, embed_image, embed_catalog_item, embed_text_clip

# When DATA_BACKEND=sql, vector search happens IN POSTGRES (pgvector) instead
# of the in-memory NumPy matrix below — see _do_visual_search and
# _load_sql_centroids. Only imported for that backend, so the default
# (memory) path never needs SQLAlchemy/psycopg/pgvector installed.
if DATA_BACKEND == "sql":
    import products_repo_sql as sql_repo

BASE_DIR = os.path.dirname(__file__)
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")
CACHE_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

# ---- catalog vector index (loaded once at startup) ----
# Two aligned matrices, same row order:
#   _index_matrix       — fused (image+text) vectors, used for RANKING (see
#                          _rank). Validated to beat image-only recall.
#   _index_image_matrix — pure-image vectors, used only to compute a DISPLAY
#                          score (see _display_scores). A pure-image query
#                          naturally scores lower against a fused vector than
#                          against a pure-image one (fusion pulls the catalog
#                          vector toward text-embedding space), so showing the
#                          fused score directly reads as misleadingly low.
#                          Ranking never uses this matrix — only what's shown.
# Both held as contiguous, L2-normalized matrices so a query is one matmul
# (see _rank / _display_scores) instead of a Python loop.
_index_skus = np.empty(0, dtype=object)         # (N,)  sku per row
_index_matrix = np.empty((0, 0), np.float32)    # (N, D) L2-normalized, fused
_index_image_matrix = np.empty((0, 0), np.float32)  # (N, D) L2-normalized, pure image
_index_cats = np.empty(0, dtype=object)   # (N,)  category per row
_sku_category = {}                        # sku -> category
_sku_row = {}                             # sku -> row index (for _display_scores lookup)
CONF_THRESHOLD = config.CONF_THRESHOLD    # % — only auto-scope above this confidence
_SOFTMAX_T = config.SOFTMAX_T             # temperature: sharpens cosine sims into probabilities


def _image_path(p):
    fname = os.path.basename(p.get("image_url") or "") or f"{p['sku']}.png"
    return os.path.join(IMAGES_DIR, fname)


def _cache_path():
    return os.path.join(CACHE_DIR, f"index_{EMBEDDING_BACKEND}.npz")


def _normalize_rows(m):
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _build_catalog_index():
    global _index_skus, _index_matrix, _index_image_matrix, _index_cats
    start = time.time()
    all_products = get_all_products()
    products = [p for p in all_products if os.path.exists(_image_path(p))]
    want = {p["sku"] for p in products}
    cat_of = {p["sku"]: p["category"] for p in all_products}
    fp = config.index_fingerprint(catalog_hash=catalog_content_hash())

    skus = vecs = image_vecs = None
    cache = _cache_path()
    if os.path.exists(cache):
        data = np.load(cache, allow_pickle=True)
        # A missing fingerprint must be treated as a MISMATCH, not accepted —
        # an older cache with no fingerprint predates a model/fusion/catalog
        # change just as surely as one with a different fingerprint string.
        cached_fp = str(data["fingerprint"][0]) if "fingerprint" in data else None
        cached_skus = list(data["skus"])
        if cached_fp != fp:
            print(f"[startup] WARNING: index cache {os.path.basename(cache)} was built by "
                  f"{cached_fp!r}, but this process needs {fp!r}. Ignoring stale/unfingerprinted cache.")
        elif want.issubset(set(cached_skus)):
            # Reindex to exactly `want`, in `products` order — cached_skus may be
            # a SUPERSET (e.g. a smaller catalog loaded against a bigger cache);
            # do NOT let extra rows leak into the live index (they'd have no
            # category, be excluded from centroids/scoped search, and be
            # unreachable via get_products_by_skus — a confusing partial state).
            row_of = {s: i for i, s in enumerate(cached_skus)}
            order = [row_of[p["sku"]] for p in products]
            skus = [p["sku"] for p in products]
            vecs = data["vecs"][order]
            # image_vecs is optional (older caches predate dual-vector storage).
            # Must be FULLY aligned with vecs (same row count + dim) or not used
            # at all — a partial/misaligned image_vecs would silently attach the
            # wrong product's display score to a result while ranking still
            # looks fine, which is worse than no display score.
            raw_image_vecs = data["image_vecs"] if "image_vecs" in data else None
            if raw_image_vecs is not None and raw_image_vecs.shape[0] == len(cached_skus) \
                    and raw_image_vecs.shape[1] == data["vecs"].shape[1]:
                image_vecs = raw_image_vecs[order]
            elif raw_image_vecs is not None:
                print(f"[startup] WARNING: image_vecs shape {raw_image_vecs.shape} doesn't match "
                      f"vecs/skus — disabling display score for this boot (falls back to fused score).")
            print(f"[startup] loaded {len(skus)} vectors from cache ({fp}) in {time.time() - start:.2f}s"
                  f"{'' if image_vecs is not None else ' (no image_vecs — display score will use fused)'}")

    if skus is None:
        # Cache miss / stale / partial. In production this must not happen — a
        # cold start firing thousands of embed calls is a cost/latency trap.
        if config.REQUIRE_PREBUILT_INDEX:
            raise RuntimeError(
                f"No valid prebuilt index for {fp} at {cache} and index.require_prebuilt=true. "
                f"Build it offline with build_index.py and ship it."
            )
        skus, fused_out, image_out = [], [], []
        for p in products:
            # embed_catalog_item fuses in name/brand/description when
            # embedding.text_fusion is enabled (clip only); falls back to plain
            # embed_image() otherwise. The live query path below always stays
            # on plain embed_image() — only catalog vectors are fused.
            with open(_image_path(p), "rb") as f:
                fused, img = embed_catalog_item(f.read(), name=p.get("name", ""), brand=p.get("brand", ""),
                                                 description=p.get("description", ""), return_image_vec=True)
            fused_out.append(fused)
            image_out.append(img)
            skus.append(p["sku"])
        vecs = np.array(fused_out, dtype=np.float32) if fused_out else np.empty((0, 0), np.float32)
        image_vecs = np.array(image_out, dtype=np.float32) if image_out else np.empty((0, 0), np.float32)
        if len(skus):
            os.makedirs(CACHE_DIR, exist_ok=True)
            np.savez(cache, skus=np.array(skus, dtype=object), vecs=vecs, image_vecs=image_vecs,
                     fingerprint=np.array([fp], dtype=object))
        print(f"[startup] embedded {len(skus)} images ({fp}) in {time.time() - start:.2f}s (cached)")

    _index_skus = np.array(skus, dtype=object)
    _index_matrix = _normalize_rows(vecs) if len(skus) else np.empty((0, 0), np.float32)
    _index_image_matrix = _normalize_rows(image_vecs) if image_vecs is not None and len(skus) else np.empty((0, 0), np.float32)
    _index_cats = np.array([cat_of.get(s, "") for s in skus], dtype=object)
    _sku_category.update({s: cat_of.get(s, "") for s in skus})
    _sku_row.clear()
    _sku_row.update({s: i for i, s in enumerate(skus)})
    print(f"[startup] backends: embedding={EMBEDDING_BACKEND}, data={DATA_BACKEND} — "
          f"index ready: {_index_matrix.shape} (display scores: {'pure-image' if _index_image_matrix.size else 'fused (no image_vecs)'})")


# ---- soft category classifier (nearest-centroid over the catalog vectors) ----
# Each category's centroid is the mean of its (normalized) product vectors. A
# query is classified by its closest centroid. Used to *softly* scope visual
# search when confident — never a hard gate (low confidence searches everything).
_centroid_cats = np.empty(0, dtype=object)     # (C,) category labels
_centroid_matrix = np.empty((0, 0), np.float32)  # (C, D) L2-normalized centroids


def _build_category_centroids():
    global _centroid_cats, _centroid_matrix
    if _index_matrix.size == 0:
        return
    cats = sorted(set(_index_cats.tolist()) - {""})
    rows = []
    for c in cats:
        mask = _index_cats == c
        rows.append(_index_matrix[mask].mean(axis=0))
    _centroid_cats = np.array(cats, dtype=object)
    _centroid_matrix = _normalize_rows(np.array(rows, dtype=np.float32))
    print(f"[startup] built {len(cats)} category centroids for the soft classifier")


def _load_sql_centroids():
    """DATA_BACKEND=sql equivalent of _build_category_centroids — loads the
    precomputed centroids from Postgres (13-row SELECT) instead of averaging
    the in-memory matrix, since that matrix doesn't exist under this backend.
    Populates the SAME globals, so _classify() is unchanged either way."""
    global _centroid_cats, _centroid_matrix
    centroids = sql_repo.get_category_centroids()
    if not centroids:
        print("[startup] WARNING: no category centroids in Postgres — run "
              "products_repo_sql.init_and_seed() first. Classifier disabled until then.")
        return
    cats = sorted(centroids)
    _centroid_cats = np.array(cats, dtype=object)
    _centroid_matrix = _normalize_rows(np.array([centroids[c] for c in cats], dtype=np.float32))
    print(f"[startup] loaded {len(cats)} category centroids from Postgres for the soft classifier")


def _classify(qn_vec):
    """qn_vec must be L2-normalized. Returns {category, confidence(%), ranking}."""
    if _centroid_matrix.size == 0:
        return None
    sims = _centroid_matrix @ qn_vec           # (C,) cosine (both normalized)
    z = sims / _SOFTMAX_T
    z -= z.max()
    e = np.exp(z)
    probs = e / e.sum()
    order = np.argsort(probs)[::-1]
    return {
        "category": str(_centroid_cats[order[0]]),
        "confidence": round(float(probs[order[0]]) * 100, 1),
        "ranking": [(str(_centroid_cats[i]), round(float(probs[i]) * 100, 1)) for i in order[:3]],
    }


def _rank(qn_vec, k, category=None):
    """Vectorized top-k over the (optionally category-scoped) index.
    qn_vec must be L2-normalized. Returns [(sku, score), ...] desc."""
    if _index_matrix.size == 0:
        return []
    if category is not None:
        mask = _index_cats == category
        M = _index_matrix[mask]
        skus = _index_skus[mask]
    else:
        M, skus = _index_matrix, _index_skus
    if M.shape[0] == 0:
        return []
    scores = M @ qn_vec                        # (n,) cosine similarities
    k = min(k, scores.shape[0])
    top = np.argpartition(-scores, k - 1)[:k]  # unordered top-k
    top = top[np.argsort(-scores[top])]        # order them
    return [(str(skus[i]), float(scores[i])) for i in top]


def _display_scores(qn_vec, skus):
    """Pure-image cosine for the given skus, for DISPLAY only — ranking always
    uses the fused index (_rank), never this. Falls back silently (empty dict,
    caller keeps the fused score) if pure-image vectors aren't available
    (e.g. an older cache, or the heuristic/vertex backends with fusion off).
    Cheap: only computed for the small returned top-k, not the whole catalog."""
    if _index_image_matrix.size == 0:
        return {}
    out = {}
    for s in skus:
        i = _sku_row.get(s)
        if i is not None:
            out[s] = float(_index_image_matrix[i] @ qn_vec)
    return out


# Minimal valid 1x1 PNG, used only to force the CLIP model to load at startup
# (see lifespan below) rather than on whichever user's request happens to
# arrive first.
_WARMUP_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00'
               b'\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00'
               b'\x00\x00IEND\xaeB`\x82')


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATA_BACKEND == "sql":
        # Vectors live in Postgres — no in-memory index to build. Only the
        # small classifier centroids are loaded into memory (see
        # _load_sql_centroids); _do_visual_search routes to
        # sql_repo.search_by_vector() instead of _rank/_display_scores.
        _load_sql_centroids()
        print(f"[startup] backends: embedding={EMBEDDING_BACKEND}, data={DATA_BACKEND} — "
              f"vector search delegated to Postgres/pgvector (no in-memory index)")
    else:
        _build_catalog_index()
        _build_category_centroids()

    if EMBEDDING_BACKEND == "clip":
        # Force the (otherwise lazily-loaded) model to load now: a cold load
        # is a 5-15s disk read + torch init, and without this it happens
        # inside whichever request is first to reach the process — visibly,
        # during a live demo. embeddings._get_clip() is lock-guarded, so this
        # also closes the window where two near-simultaneous first-requests
        # would each load their own full copy of the model.
        t0 = time.time()
        await run_in_threadpool(embed_image, _WARMUP_PNG)
        print(f"[startup] warmed CLIP model in {time.time() - t0:.2f}s")

    yield


app = FastAPI(title="Staples Visual Search Demo", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,   # "*" for local demo; lock down for public deploy
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_app_code(request, call_next):
    """Keep the app's HTML/CSS/JS always fresh (no stale UI after an edit).
    Product images under /images keep normal caching."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/") or path.endswith(".html") or path == "/":
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _serialize(p):
    list_price = p.get("list_price") or p.get("price") or 0
    savings = round(100 * (list_price - p["price"]) / list_price) if list_price > 0 else 0
    image_url = p.get("image_url") or f"/images/products/{p['sku']}.png"
    if config.IMAGES_BASE_URL and image_url.startswith("/images/products/"):
        # Point straight at the CDN instead of this container's own
        # same-origin path. A bare 302 (see redirect_product_image below)
        # isn't cacheable by browsers, so leaving this as a relative path
        # would mean every image on every page view re-hits Cloud Run once
        # just to be told where the real bytes are -- exactly the
        # CPU/concurrency contention IMAGES_BASE_URL exists to avoid.
        image_url = f"{config.IMAGES_BASE_URL}/products/{image_url.rsplit('/', 1)[-1]}"
    return {
        **p,
        "image_url": image_url,
        "savings_pct": max(savings, 0),
    }


def _serialize_bundle_items(container, *keys):
    """In-place: replace container[key] with _serialize(container[key]) for
    each key present. agent.py's bundling tools (plan_office_setup,
    plan_room_setup, swap_bundle_item) return raw catalog rows via
    get_products_by_category/get_product_by_sku, not the _serialize()'d
    ones every other tool's card data goes through (_hybrid_search already
    serializes internally) -- skipping this left every bundle/swap card
    showing "You save undefined%" and its image bypassing the CDN redirect."""
    for key in keys:
        item = container.get(key)
        if item:
            container[key] = _serialize(item)


# ---------- API ----------

_MAX_PAGE_SIZE = 200


def _page(items, limit, offset):
    """Slice a result list into a page and report the true total. limit/offset
    are clamped so a request can't force the whole catalog into one response
    (?limit=-1 previously returned everything but one item; ?limit=999999 was
    unbounded)."""
    total = len(items)
    offset = max(int(offset), 0)
    limit = max(1, min(int(limit), _MAX_PAGE_SIZE)) if limit is not None else _MAX_PAGE_SIZE
    window = items[offset: offset + limit]
    return total, window


def _quality_key(p):
    """Same review-count-shrunk formula as agent.py's _quality_score (kept
    as a separate one-line copy rather than a cross-module import -- this
    is a plain listing-sort helper, not part of the chat agent)."""
    rating = p.get("rating") or 0
    reviews = p.get("reviews") or 0
    return rating * reviews / (reviews + 20)


def _deals_key(p):
    list_price = p.get("list_price") or 0
    return (list_price - p.get("price", 0)) / list_price if list_price > 0 else 0.0


@app.get("/api/products")
def list_products(category: str | None = None, limit: int | None = config.PAGE_SIZE, offset: int = 0,
                   sort: str | None = None):
    """sort: None (default) | "deals" | "popular" -- see get_products_page's
    docstring for why this exists (the homepage's "Deals"/"Popular" rails
    used to be two arbitrary slices of the same unsorted list)."""
    # Clamped once, above the branch, so both backends echo the SAME limit
    # back in the response -- the sql branch used to clamp only on its own
    # side, so ?limit=999999 on the memory backend echoed the raw value
    # back while actually returning a normal-sized page.
    offset = max(int(offset), 0)
    limit = max(1, min(int(limit), _MAX_PAGE_SIZE)) if limit is not None else _MAX_PAGE_SIZE
    if DATA_BACKEND == "sql":
        # Paginated in SQL (LIMIT/OFFSET) — never fetches the whole table just
        # to slice one page of it (see get_products_page's docstring).
        total, window = sql_repo.get_products_page(category, limit, offset, sort)
    else:
        items = get_products_by_category(category) if category else get_all_products()
        if sort == "deals":
            items = sorted(items, key=_deals_key, reverse=True)
        elif sort == "popular":
            items = sorted(items, key=_quality_key, reverse=True)
        total, window = _page(items, limit, offset)
    return {"count": total, "offset": offset, "limit": limit,
            "items": [_serialize(p) for p in window]}


@app.get("/api/products/{sku}")
def get_product(sku: str):
    p = get_product_by_sku(sku)
    if not p:
        raise HTTPException(404, "Product not found")
    return _serialize(p)


@app.get("/api/categories")
def list_categories():
    cats = sql_repo.get_categories() if DATA_BACKEND == "sql" else sorted({p["category"] for p in get_all_products()})
    return {"categories": cats}


HYBRID_SEARCH_POOL = 100   # depth each side (keyword, semantic) contributes before RRF-fusing


def _semantic_search_skus(query_text, k=HYBRID_SEARCH_POOL):
    """Ordered sku list from ranking the query's CLIP TEXT embedding against
    the catalog's FUSED vectors (not pure-image) — with text_fusion enabled,
    those vectors are already ~70% the CLIP text embedding of each product's
    own name/brand/description, so a text query against them is a much
    stronger signal than against pure-image vectors would be.

    Returns None (not an empty list) when the clip backend isn't active, so
    callers can tell "no text tower available" apart from "found nothing" —
    heuristic/vertex don't have embed_text_clip (same gate the refine-by-text
    feature already uses for the same reason)."""
    if EMBEDDING_BACKEND != "clip":
        return None
    text_vec = embed_text_clip(query_text[:200])
    qn = text_vec / (np.linalg.norm(text_vec) + 1e-8)
    if DATA_BACKEND == "sql":
        ranked = sql_repo.search_by_vector(qn.tolist(), k=k)
        return [sku for sku, _rank_score, _disp in ranked]
    return [sku for sku, _score in _rank(qn, k)]


def _rrf_fuse(named_rankings, k=60, limit=HYBRID_SEARCH_POOL):
    """Reciprocal Rank Fusion: combine independently-ranked sku lists into
    one, without needing their scores to be on the same scale — they
    aren't. LIKE-matching has no continuous score at all, and CLIP
    text-image cosines aren't calibrated against keyword-match strength, so
    blending the two as numbers would be meaningless. Each list contributes
    1/(k+rank) per sku it contains; a sku ranked near the top of either
    list — or appearing in both — rises to the top of the fused result.

    named_rankings: [(source_name, [sku, ...]), ...], each list best-first.
    Returns [(sku, rrf_score, {source_name, ...}), ...] sorted desc — the
    source set is what powers the "keyword"/"semantic"/"both" match_reason
    badge in the frontend."""
    scores, sources = {}, {}
    for name, ranking in named_rankings:
        for i, sku in enumerate(ranking):
            scores[sku] = scores.get(sku, 0.0) + 1.0 / (k + i + 1)
            sources.setdefault(sku, set()).add(name)
    ranked = sorted(scores, key=scores.get, reverse=True)[:limit]
    return [(sku, scores[sku], sources[sku]) for sku in ranked]


def _hybrid_search(q, limit=config.PAGE_SIZE, offset=0, max_price=None):
    """Hybrid keyword + semantic search — the actual logic behind /api/search,
    factored out so backend/agent.py's search_products tool can call it
    in-process (no HTTP hop to itself) instead of duplicating it.

    The existing LIKE-based keyword match (ranked by name-hit relevance
    rather than arbitrary sku order — see search_products_ranked_skus /
    _mem_search) is fused via Reciprocal Rank Fusion with a CLIP-text-tower
    semantic ranking against the catalog's fused vectors. Keyword alone
    misses anything phrased differently than the catalog text ("something
    to print with" matches nothing containing "print"); semantic alone can
    rank a literal exact-name hit below something merely topically similar.
    RRF gets both without trying to compare their scores directly (not
    meaningful — see _rrf_fuse). Falls back to keyword-only, still with the
    relevance-order fix, when the active embedding backend has no text
    tower.

    max_price: applied BEFORE pagination, not after -- filtering a single
    already-fetched page client-side (the frontend's price-intent parser,
    _parsePriceIntent, already existed for visual-search refinement, but
    plain text search had nothing wired to it: "desks under $200" ignored
    the budget entirely) would silently shrink individual pages and desync
    `count`/offset from what's actually shown."""
    q = (q or "").strip()
    limit = max(1, min(int(limit), _MAX_PAGE_SIZE)) if limit is not None else _MAX_PAGE_SIZE
    offset = max(int(offset), 0)
    if not q:
        return {"count": 0, "query": q, "offset": offset, "limit": limit, "items": []}

    if DATA_BACKEND == "sql":
        keyword_skus = sql_repo.search_products_ranked_skus(q, limit=HYBRID_SEARCH_POOL)
    else:
        keyword_skus = [p["sku"] for p in search_products(q)][:HYBRID_SEARCH_POOL]

    semantic_skus = _semantic_search_skus(q)
    rankings = [("keyword", keyword_skus)]
    if semantic_skus is not None:
        rankings.append(("semantic", semantic_skus))
    fused = _rrf_fuse(rankings)

    # RRF fuses a bounded pool (HYBRID_SEARCH_POOL per side) rather than the
    # whole catalog, so `count` here is that fused pool's size, not a true
    # total match count — same "bounded, not silently unbounded" tradeoff
    # REFINE_POOL_SIZE already makes elsewhere in this app.
    pool_by_sku = None
    if max_price is not None:
        # Fetch the whole (bounded) fused pool once to filter by price
        # before windowing -- reused below instead of a second lookup.
        pool_by_sku = get_products_by_skus([sku for sku, _score, _sources in fused])
        fused = [(sku, score, sources) for sku, score, sources in fused
                 if pool_by_sku.get(sku) and pool_by_sku[sku]["price"] <= max_price]

    total = len(fused)
    window = fused[offset:offset + limit]
    by_sku = pool_by_sku if pool_by_sku is not None else get_products_by_skus([sku for sku, _score, _sources in window])
    items = []
    for sku, _score, sources in window:
        p = by_sku.get(sku)
        if p:
            item = _serialize(p)
            item["match_reason"] = "both" if len(sources) > 1 else next(iter(sources))
            items.append(item)

    return {"count": total, "query": q, "offset": offset, "limit": limit, "items": items}


@app.get("/api/search")
def text_search(q: str = "", limit: int | None = config.PAGE_SIZE, offset: int = 0, max_price: float | None = None):
    """See _hybrid_search for the actual logic."""
    return _hybrid_search(q, limit, offset, max_price)


@app.get("/api/config")
def app_config():
    """Lets the frontend show which backends are live (demo transparency),
    plus enough index-health signal to tell "nothing loaded yet" apart from
    "results are just weak for this photo" from the outside -- e.g.
    category_centroids == 0 under DATA_BACKEND=sql means init_and_seed()
    was never run, which otherwise only surfaces as a startup log line
    (_load_sql_centroids) and a confusing "no furnishings recognized" on
    every Shop the Room photo."""
    return {
        "embedding_backend": EMBEDDING_BACKEND,
        "data_backend": DATA_BACKEND,
        "category_centroids": int(_centroid_cats.shape[0]),
        "index_size": int(_index_skus.shape[0]) if DATA_BACKEND != "sql" else None,
    }


_MAX_UPLOAD_BYTES = int(config.MAX_UPLOAD_MB * 1024 * 1024)


REFINE_IMAGE_WEIGHT = 0.7   # query-side image/text blend when refine_text is given
REFINE_POOL_SIZE = 100      # candidate pool (by pure-image similarity) a text refinement re-ranks within


def _search_matches(qn_image, qn_blend, category, top_k):
    """Runs one search and returns [(sku, display_score), ...] desc.

    Without qn_blend: ranks directly by the pure-image vector, as before.

    With qn_blend: a text refinement ("but in black") must never be allowed
    to search the WHOLE catalog with the blended vector — a bare color word
    embeds close to generic solid-color/swatch imagery in CLIP's text space,
    and blending even 30% of that in was enough to jump a real search
    entirely out of "clock" territory and onto color swatches (found via
    manual testing: a wall-clock photo with no strong image-only match, then
    refined with "but in black", returned Moss/Forest/Natural fabric swatches
    instead of anything clock-shaped). Two-stage fix: first find a pool of
    items the NORMAL (unrefined) search would already return — i.e. ranked by
    the fused vector, generous k — then re-rank ONLY that pool by the blended
    vector. A refinement can now only reorder items that were already
    topically relevant — never conjure an unrelated one.

    The SQL backend's second stage deliberately does NOT run a second
    pgvector query: `ORDER BY embedding <=> :q LIMIT k WHERE sku IN (...)`
    would let Postgres's planner choose an HNSW index scan for that query,
    and HNSW is approximate — it only walks `ef_search` candidates, and if
    too few of those overlap the pool you can silently get fewer than top_k
    results (or zero), with no error. Fetching the pool's own vectors once
    and reranking them in NumPy is exact, small (~100 rows), and identical
    in behavior to the in-memory backend's path below — no ambiguity about
    what the query planner decides to do.
    """
    if DATA_BACKEND == "sql":
        if qn_blend is not None:
            pool = sql_repo.search_by_vector(qn_image.tolist(), category=category, k=REFINE_POOL_SIZE)
            pool_skus = [s for s, _rank_score, _disp in pool]
            if not pool_skus:
                return []
            vecs = sql_repo.get_embeddings_by_skus(pool_skus)
            fused_scores = {sku: float(fused @ qn_blend) for sku, (fused, _img) in vecs.items()}
            top_skus = sorted(fused_scores, key=fused_scores.get, reverse=True)[:top_k]
            return [(sku, float(vecs[sku][1] @ qn_image)) for sku in top_skus]
        else:
            ranked = sql_repo.search_by_vector(qn_image.tolist(), category=category, k=top_k)
        return [(sku, display_score) for sku, _rank_score, display_score in ranked]

    if qn_blend is not None:
        pool = _rank(qn_image, REFINE_POOL_SIZE, category=category)
        pool_idx = [_sku_row[s] for s, _ in pool if s in _sku_row]
        if not pool_idx:
            return []
        sub_matrix, sub_skus = _index_matrix[pool_idx], _index_skus[pool_idx]
        scores = sub_matrix @ qn_blend
        k2 = min(top_k, scores.shape[0])
        top = np.argpartition(-scores, k2 - 1)[:k2]
        top = top[np.argsort(-scores[top])]
        matches = [(str(sub_skus[i]), float(scores[i])) for i in top]
    else:
        matches = _rank(qn_image, top_k, category=category)

    # RANKING (order + which items) always comes from the fused/blended score
    # above. What's DISPLAYED (and what the match-threshold gate reacts to) is
    # the pure-image score instead: more interpretable, since a pure-image
    # query naturally scores lower against a fused catalog vector than a
    # pure-image one.
    disp = _display_scores(qn_image, [s for s, _ in matches])
    return [(s, disp.get(s, sc)) for s, sc in matches]


def _do_visual_search(image_bytes, top_k, scope, refine_text=""):
    """CPU-bound work — embed + classify + rank. Runs in a threadpool so it never
    blocks the event loop (see the endpoint)."""
    query_vec = embed_image(image_bytes)
    qn_image = query_vec / (np.linalg.norm(query_vec) + 1e-8)   # normalize the query ONCE

    # Classification always uses the PURE image vector — a text refinement
    # like "but in black" shouldn't change what category we think the photo
    # is, only which of that category's items rise to the top. (Previously
    # this classified on the blended vector, which made confidence drift on
    # every refinement for no real reason.)
    cls = _classify(qn_image) if config.CLASSIFIER_ENABLED else None
    category, scoped = None, False
    if cls and (scope == "force" or (scope == "auto" and cls["confidence"] >= CONF_THRESHOLD)):
        category, scoped = cls["category"], True

    # Text-refined query ("but in black"): blend the photo's embedding with a
    # CLIP text embedding of the refinement, in the SAME joint space
    # embed_catalog_item() already fuses catalog vectors in. See
    # _search_matches for why this blend is constrained to a photo-similar
    # pool rather than searching the whole catalog with it directly.
    qn_blend = None
    if refine_text and EMBEDDING_BACKEND == "clip":
        text_vec = embed_text_clip(refine_text[:200])
        tn = text_vec / (np.linalg.norm(text_vec) + 1e-8)
        qn_blend = REFINE_IMAGE_WEIGHT * qn_image + (1 - REFINE_IMAGE_WEIGHT) * tn
        qn_blend = qn_blend / (np.linalg.norm(qn_blend) + 1e-8)

    matches = _search_matches(qn_image, qn_blend, category, top_k)
    if scoped and not matches:              # empty scoped result → fall back to full search
        category, scoped = None, False
        matches = _search_matches(qn_image, qn_blend, None, top_k)

    if DATA_BACKEND == "sql":
        searched = sql_repo.count_by_category(category)
    else:
        searched = int((_index_cats == category).sum()) if category is not None else int(_index_skus.shape[0])
    # Under a text refinement, only the REFINE_POOL_SIZE-deep pool actually
    # gets reranked (see _search_matches) — reporting the full catalog/
    # category count here would overstate it in an app whose whole pitch is
    # calibrated honesty about what was actually searched.
    if qn_blend is not None:
        searched = min(searched, REFINE_POOL_SIZE)

    return {"cls": cls, "scoped": scoped, "matches": matches, "searched": searched}


def _get_pure_image_vector(sku):
    """Pure-image vector for an existing catalog product, L2-normalized, or
    None if the sku is unknown."""
    if DATA_BACKEND == "sql":
        qvec = sql_repo.get_image_embedding(sku)
        if qvec is None:
            return None
        return qvec / (np.linalg.norm(qvec) + 1e-8)
    else:
        i = _sku_row.get(sku)
        if i is None:
            return None
        qn = _index_image_matrix[i] if _index_image_matrix.size else _index_matrix[i]
        return qn / (np.linalg.norm(qn) + 1e-8)


def _do_similar_search(sku, top_k, refine_text=""):
    """'Find similar' — query FROM an existing catalog product's own image
    vector instead of an uploaded photo. No re-embedding needed (the vector
    is already stored); reuses _search_matches, the exact same machinery a
    real visual search uses, INCLUDING its pool-constrained text-refinement
    safety when refine_text is given — "find similar, but in black" behaves
    identically to refining an uploaded photo, not a separate code path.
    Returns None if sku is unknown, else [(sku, display_score), ...] with
    the source product excluded."""
    qn_image = _get_pure_image_vector(sku)
    if qn_image is None:
        return None

    qn_blend = None
    if refine_text and EMBEDDING_BACKEND == "clip":
        text_vec = embed_text_clip(refine_text[:200])
        tn = text_vec / (np.linalg.norm(text_vec) + 1e-8)
        qn_blend = REFINE_IMAGE_WEIGHT * qn_image + (1 - REFINE_IMAGE_WEIGHT) * tn
        qn_blend = qn_blend / (np.linalg.norm(qn_blend) + 1e-8)

    matches = _search_matches(qn_image, qn_blend, None, top_k + 1)
    return [(s, score) for s, score in matches if s != sku][:top_k]


@app.get("/api/products/{sku}/similar")
def similar_products(sku: str, top_k: int = 12, refine_text: str = ""):
    """Visually similar products to an existing catalog item — powers "find
    similar" on product cards/PDPs without requiring an upload. refine_text
    works the same as it does for a real visual search (see _do_visual_search)."""
    top_k = max(1, min(int(top_k), 50))
    matches = _do_similar_search(sku, top_k, (refine_text or "").strip())
    if matches is None:
        raise HTTPException(404, "Product not found")
    by_sku = get_products_by_skus([s for s, _ in matches])
    results = []
    for s, score in matches:
        p = by_sku.get(s)
        if p:
            item = _serialize(p)
            item["match_score"] = round(score * 100, 1)
            results.append(item)
    # _search_matches RANKS by the fused-vector score but this endpoint
    # DISPLAYS the pure-image score instead (see _display_scores) -- two
    # different metrics that don't always agree, so the list order (by
    # rank score) and the visible percentages (display score) could
    # legitimately disagree, e.g. a displayed 75% sitting below a 67.6%.
    # Correct by design, but reads as a sorting bug to anyone just looking
    # at the numbers on screen -- if a percentage is shown, the list
    # should be ordered by it.
    results.sort(key=lambda r: -r["match_score"])
    return {"count": len(results), "items": results, "refine_text": refine_text or None}


COMPLETE_LOOK_MAX_ITEMS = 4   # a cross-sell rail reads best small and tight — a few
                               # coordinated items, not a full category dump


def _do_complete_the_look(sku, max_items=COMPLETE_LOOK_MAX_ITEMS):
    """"Complete the look": given ONE product, find the single best-matching
    item in each OTHER category — so a strongly-styled product (rattan
    chair, mid-century walnut desk) surfaces a visually coherent set to pair
    it with, e.g. the lamp/rug/side-table that goes with it. One match per
    CATEGORY, not "customers also bought" and not per-tile like Shop the
    Room — this is the same per-category search Shop the Room already uses
    (_search_matches restricted to a category, top_k=1), just looped over
    the catalog's own category list instead of image tiles, starting from
    one product's already-stored vector (_get_pure_image_vector) instead of
    re-embedding a photo. No new model, no new index.

    Deliberately does NOT apply MATCH_THRESHOLD here: that gate is
    calibrated for "is this the same object as the query" (same-category
    identification), and a chair's vector is structurally farther from a
    lamp's than from another chair's even when they're a genuinely
    coordinated pair — gating on that score would empty out most
    categories rather than surface real style-coherence. Top-1-per-category
    is itself already a strong filter (the single closest thing available),
    so nothing here is unfiltered.

    Returns None if sku is unknown, else [(sku, score), ...] sorted best
    first, excluding the source product's own category.
    """
    qn = _get_pure_image_vector(sku)
    if qn is None:
        return None
    source = get_product_by_sku(sku)
    source_category = source["category"] if source else None

    categories = sql_repo.get_categories() if DATA_BACKEND == "sql" else sorted({p["category"] for p in get_all_products()})
    results = []
    for category in categories:
        if category == source_category:
            continue
        matches = _search_matches(qn, None, category, 1)
        if matches:
            results.append(matches[0])

    results.sort(key=lambda t: -t[1])
    return results[:max_items]


@app.get("/api/products/{sku}/complete-the-look")
def complete_the_look(sku: str, max_items: int = COMPLETE_LOOK_MAX_ITEMS):
    """Cross-sell rail: coordinated items from OTHER categories for a given
    product — see _do_complete_the_look."""
    max_items = max(1, min(int(max_items), 12))
    matches = _do_complete_the_look(sku, max_items)
    if matches is None:
        raise HTTPException(404, "Product not found")
    by_sku = get_products_by_skus([s for s, _ in matches])
    results = []
    for s, score in matches:
        p = by_sku.get(s)
        if p:
            item = _serialize(p)
            item["match_score"] = round(score * 100, 1)
            results.append(item)
    return {"count": len(results), "items": results}


SHOP_ROOM_GRID = 2          # NxN tile sweep of the uploaded photo — kept small
                            # (5 embed+search calls total: whole image + 4
                            # tiles) since DATA_BACKEND=sql pays one network
                            # round trip per tile; measured 3x3 (10 calls) at
                            # ~30s cross-region in testing, vs. same-region
                            # Cloud Run->Cloud SQL being far faster per earlier
                            # single-search timing (~0.4-1.2s) -- 2x2 keeps
                            # worst-case latency bounded without a redesign.
SHOP_ROOM_MAX_ITEMS = 6     # cap on distinct categories returned
# A dedicated, slightly lower match-score gate for Shop the Room specifically
# -- verified live on the app's own homepage sample photo (room-1.jpg): the
# whole-image tile correctly classified a large grey sofa as "sofas" (this
# feature already doesn't gate on classifier confidence, see the docstring
# below), but its best catalog match scored 52.1%, just under the global
# MATCH_THRESHOLD (55.0) calibrated for single-object searches. A tile crop
# from a real lifestyle photo is inherently a weaker, more indirect query
# than a clean single-object photo, so a small carve-out here is consistent
# with the confidence carve-out this feature already has -- NOT a change to
# the global threshold every other search in the app is calibrated against.
SHOP_ROOM_MATCH_THRESHOLD = 50.0


def _tile_crops(image_bytes, grid=SHOP_ROOM_GRID):
    """Splits a photo into an NxN grid of square-ish crops, plus the whole
    image (index 0) for context. Each crop is re-encoded as JPEG bytes so it
    can go through the exact same embed_image() call as a normal upload —
    no new model, no new preprocessing path."""
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = im.size
    tiles = [image_bytes]
    tw, th = w / grid, h / grid
    for r in range(grid):
        for c in range(grid):
            box = (c * tw, r * th, (c + 1) * tw, (r + 1) * th)
            buf = io.BytesIO()
            im.crop(box).save(buf, format="JPEG", quality=90)
            tiles.append(buf.getvalue())
    return tiles


def _do_shop_the_room(image_bytes):
    """"Shop the room": one photo of a room -> one best-matching product PER
    DISTINCT category detected in it (the sofa, the rug, the lamp, the wall
    art), instead of a pile of near-duplicates of whatever's most prominent.

    No new model or endpoint dependency: sweeps the photo into a grid of
    tiles (_tile_crops), and for each tile reuses exactly the same pipeline
    a normal search already uses — embed_image, _classify, and
    _search_matches restricted to that tile's predicted category, top_k=1.

    Unlike a normal search, a tile's classification does NOT need to clear
    CONF_THRESHOLD — that threshold is calibrated for a different decision
    ("should we auto-scope a WHOLE photo's search to one category"), and a
    smaller, partially-cropped tile is inherently less certain even when its
    top-1 category guess is correct (verified: a lamp tile classified at only
    36% confidence, well under the 45% auto-scope gate, but its top-1
    category guess was still right and the resulting product match scored
    77%). What actually matters here is whether the tile's TOP PRODUCT MATCH
    clears SHOP_ROOM_MATCH_THRESHOLD — the same kind of "is this actually a
    good match" gate every other search in this app uses, but a dedicated,
    slightly lower one (see its definition above) rather than the global
    MATCH_THRESHOLD, since a tile crop is a structurally weaker query than a
    clean single-object photo. Tiles whose top match doesn't clear it (a
    blank wall, empty floor, nothing catalog-like) are simply skipped.
    Multiple tiles landing on the same category (e.g. two tiles both seeing
    the same sofa) keep only the best-scoring one.
    """
    best_by_category = {}   # category -> (score, sku)
    for tile_bytes in _tile_crops(image_bytes):
        query_vec = embed_image(tile_bytes)
        qn = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        cls = _classify(qn) if config.CLASSIFIER_ENABLED else None
        if not cls:
            continue
        category = cls["category"]
        matches = _search_matches(qn, None, category, 1)
        if not matches:
            continue
        sku, score = matches[0]
        if score * 100 < SHOP_ROOM_MATCH_THRESHOLD:
            continue
        prev = best_by_category.get(category)
        if prev is None or score > prev[0]:
            best_by_category[category] = (score, sku)

    ranked = sorted(best_by_category.items(), key=lambda kv: -kv[1][0])[:SHOP_ROOM_MAX_ITEMS]
    return [(sku, score) for _category, (score, sku) in ranked]


@app.post("/api/shop-the-room")
async def shop_the_room(request: Request, file: UploadFile = File(...)):
    """One room photo in, one best-matching product per detected category
    out, with a running basket total — see _do_shop_the_room."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Please upload an image file")

    clen = request.headers.get("content-length")
    if clen:
        try:
            clen_int = int(clen)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length header")
        if clen_int > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")

    image_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file")
    if len(image_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")

    _t0 = time.time()
    try:
        matches = await run_in_threadpool(_do_shop_the_room, image_bytes)
    except OSError:
        # PIL couldn't decode the upload -- the one failure mode this message
        # actually describes. Anything else (a DB/ranking error mid-request)
        # is a real server-side problem, not a bad image, and shouldn't be
        # mislabeled as one or echo internal exception text to the client.
        raise HTTPException(400, "Could not process image. Try a different image file.")
    took_ms = round((time.time() - _t0) * 1000)

    by_sku = await run_in_threadpool(get_products_by_skus, [sku for sku, _ in matches])
    results, total = [], 0.0
    for sku, score in matches:
        p = by_sku.get(sku)
        if p:
            item = _serialize(p)
            item["match_score"] = round(score * 100, 1)
            results.append(item)
            total += p["price"]

    return {
        "count": len(results),
        "items": results,
        "total_price": round(total, 2),
        "took_ms": took_ms,
    }


@app.post("/api/visual-search")
async def visual_search(request: Request, file: UploadFile = File(...),
                        top_k: int = config.TOP_K, scope: str = "auto", refine_text: str = ""):
    """Visual search with soft category scoping.

    scope: "auto"  (default) — scope to the predicted category only when confident
           "all"   — never scope (search the whole catalog)
           "force" — always scope to the predicted category
    refine_text: optional free-text refinement ("but in black", "cheaper") —
        blended into the query embedding before ranking. Only has an effect
        on the clip backend (silently ignored otherwise, see _do_visual_search).
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Please upload an image file")

    top_k = max(1, min(int(top_k), 100))   # a negative/huge top_k previously returned the whole catalog
    refine_text = (refine_text or "").strip()

    # Reject clearly-oversized uploads up front (before reading the body).
    clen = request.headers.get("content-length")
    if clen:
        try:
            clen_int = int(clen)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length header")
        if clen_int > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")

    image_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)   # bounded read (backstop; don't buffer huge uploads)
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file")
    if len(image_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")

    _t0 = time.time()
    try:
        r = await run_in_threadpool(_do_visual_search, image_bytes, top_k, scope, refine_text)
    except OSError:
        # See the matching comment in shop_the_room above.
        raise HTTPException(400, "Could not process image. Try a different image file.")
    took_ms = round((time.time() - _t0) * 1000)

    cls, matches = r["cls"], r["matches"]
    by_sku = await run_in_threadpool(get_products_by_skus, [sku for sku, _ in matches])   # single batch fetch (no N+1)
    results = []
    for sku, score in matches:
        p = by_sku.get(sku)
        if p:
            item = _serialize(p)
            item["match_score"] = round(score * 100, 1)
            results.append(item)
    # Same rank-vs-display-score split as similar_products -- see that
    # endpoint's comment. Order by what's actually shown.
    results.sort(key=lambda r: -r["match_score"])

    return {
        "count": len(results),
        "items": results,
        "predicted_category": cls["category"] if cls else None,
        "confidence": cls["confidence"] if cls else None,
        "category_ranking": cls["ranking"] if cls else None,
        "scoped": r["scoped"],
        "searched": r["searched"],   # how many vectors were actually ranked
        "took_ms": took_ms,          # embed + rank time (excludes upload/network)
        "refine_text": refine_text or None,
        "match_threshold": config.MATCH_THRESHOLD,   # results below this % are "weak"
        # "image" = displayed % is pure-image cosine (match_threshold's calibrated
        # scale); "fused" = no valid image_vecs this boot, displayed % is the raw
        # fused score instead (reads much lower for the same true match quality —
        # match_threshold is NOT calibrated for this scale). Under DATA_BACKEND=sql
        # the display score always comes from pgvector's image_embedding column
        # (search_by_vector always returns it), so it's "image" whenever that
        # backend is active; under memory it depends on whether image_vecs loaded.
        "score_basis": "image" if (DATA_BACKEND == "sql" or _index_image_matrix.size) else "fused",
    }


# ---------- Staples AI chat agent ----------

class AgentChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    # SKU/name/price/category of whatever product cards are currently
    # rendered in the chat panel (the frontend resends its last response's
    # `items` verbatim) -- lets the model resolve "the second one" / "cheaper
    # than that" / "just the chair" without re-searching blind. See
    # _build_screen_context. Untrusted client input: only the four fields
    # below are ever read out of each entry.
    last_items: list[dict] = []
    # Optional photo attached to THIS turn only (base64-encoded, no data:
    # URL prefix) -- e.g. a room to furnish (routes to shop_the_room) or a
    # photo of a list/note/receipt (routes to match_shopping_list). Not
    # carried into history for later turns; see _do_agent_chat.
    image_b64: str | None = None
    image_mime: str | None = None


def _history_to_contents(history):
    from vertexai.generative_models import Content, Part
    contents = []
    for turn in history:
        role = "model" if turn.get("role") == "model" else "user"
        text = str(turn.get("text", "")).strip()
        if text:
            contents.append(Content(role=role, parts=[Part.from_text(text)]))
    return contents


def _build_screen_context(last_items):
    """A short "here's what the user is currently looking at" preamble,
    prepended to the user's new message (same turn, not a separate one --
    avoids fussing with Gemini's alternating-role turn requirement). Without
    this, the model never learns the price/sku of anything it showed (the
    system prompt deliberately tells it not to state prices itself), so a
    follow-up like "cheaper than that" or "just the chair" has nothing to
    resolve against."""
    if not last_items:
        return ""
    lines = []
    for it in last_items[:20]:
        if not isinstance(it, dict) or not it.get("sku"):
            continue
        name = str(it.get("name", ""))[:120]
        price = it.get("price")
        price_str = f"${price:.2f}" if isinstance(price, (int, float)) else "price unknown"
        lines.append(f"- {name} ({price_str}, category: {it.get('category', '')}, sku: {it['sku']})")
    if not lines:
        return ""
    return (
        "[Context -- currently shown to the user on screen, not something they typed. "
        "Use this ONLY to resolve references like \"the second one\" or \"cheaper than that\"; "
        "never read this list back to the user verbatim.]\n" + "\n".join(lines) + "\n\n"
    )


_STOPWORDS = {"the", "and", "for", "with", "some", "any", "get", "buy", "need", "please"}


def _plausible_match(query, item):
    """_hybrid_search's RRF fusion has no natural reject threshold -- it
    returns SOMETHING for almost any string, since the semantic side always
    ranks the whole catalog by cosine similarity even when nothing is truly
    relevant. Taking its top-1 unfiltered meant match_shopping_list could
    confidently resolve a scribbled "milk" to an office chair, and the
    unmatched list -- meant to be an honest signal -- would almost never
    populate. Require at least one real query word (or its singular form,
    see text_match.singularize) to actually appear in the matched
    product's own name/category text, mirroring the substring logic the
    keyword side of search already uses. Imperfect (misses pure synonyms
    like "sofa" matching a product only ever named "couch"), but a real
    floor where there was none."""
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2 and w not in _STOPWORDS]
    if not words:
        return True
    haystack = f"{item.get('name', '')} {item.get('category', '')}".lower()
    return any(w in haystack or singularize(w) in haystack for w in words)


def _generate_with_retry(model, contents, **kwargs):
    """One retry on any exception, same reasoning as agent._get_agent_model's
    init retry -- a cold Vertex client's first RPC on a freshly-started
    Cloud Run instance can transiently fail. A genuinely persistent failure
    still exhausts this and correctly falls through to agent_chat's
    degraded fallback; this only absorbs the one-off case."""
    try:
        return model.generate_content(contents, **kwargs)
    except Exception:
        print(f"[agent] generate_content failed, retrying once:\n{traceback.format_exc()}", file=sys.stderr)
        time.sleep(0.75)
        return model.generate_content(contents, **kwargs)


def _do_agent_chat(message, history, last_items=None, image_bytes=None, image_mime=None):
    """Tool-calling loop: ask Gemini, and when it calls a tool, run the
    REAL implementation in-process (_hybrid_search for search_products,
    agent.plan_office_setup for plan_office_setup) and feed the real
    result back -- the model never invents product data or prices, it
    only ever narrates what these two functions actually returned.

    image_bytes (if given) is attached to THIS turn's Content alongside the
    text -- Gemini's own vision sees it directly (needed for it to decide
    "room" vs "list" and, for a list, to read the text off it itself). It is
    NOT re-attached on later turns (history is text-only), and the
    shop_the_room/match_shopping_list branches below read it from this
    function's closure, not from anything Gemini echoes back -- a model
    can decide something IS an image, but the actual bytes never round-trip
    through a function-call argument."""
    from vertexai.generative_models import Content, Part

    state = agent._get_agent_model()
    model = state["model"]
    prefixed_message = _build_screen_context(last_items) + message
    turn_parts = [Part.from_text(prefixed_message)] if prefixed_message else []
    if image_bytes:
        turn_parts.append(Part.from_data(data=image_bytes, mime_type=image_mime or "image/jpeg"))
    contents = _history_to_contents(history) + [Content(role="user", parts=turn_parts)]

    items, bundle, seen_skus = [], None, set()
    for i in range(config.AGENT_MAX_ITERATIONS):
        # On the last allowed turn, force a text answer instead of letting the
        # model start yet another tool call it'll never get to see the result
        # of -- otherwise AGENT_MAX_ITERATIONS=3 really means "2 tool calls,
        # then always apologize," even when the model already has everything
        # it needs to answer. fn_calls is hard-forced to [] here (not just
        # tools=[] on the request) so this degrades safely even if the SDK's
        # per-call tools override doesn't behave as expected.
        last_turn = (i == config.AGENT_MAX_ITERATIONS - 1)
        call_kwargs = {"tools": [] if last_turn else None}
        if i == 0 and image_bytes and not last_turn:
            # A photo with no caption (or a caption that doesn't say what to
            # do with it) shouldn't depend on the model choosing, unprompted,
            # to call a tool instead of just describing the picture in prose
            # -- that's a real failure mode: "attach a photo and nothing
            # happens." Force the FIRST turn to actually call one of the two
            # image-aware tools; the model still decides which (room vs
            # list), it just can't opt out of resolving the image against
            # the real catalog.
            try:
                from vertexai.generative_models import ToolConfig
                call_kwargs["tool_config"] = ToolConfig(
                    function_calling_config=ToolConfig.FunctionCallingConfig(
                        mode=ToolConfig.FunctionCallingConfig.Mode.ANY,
                        # not_shoppable is the escape hatch -- without it, an
                        # irrelevant photo (a pet, an unrelated screenshot)
                        # still had to be forced into shop_the_room, which
                        # doesn't return nothing, it returns nearest
                        # neighbors: confidently wrong furniture instead of
                        # an honest decline.
                        allowed_function_names=["shop_the_room", "match_shopping_list", "not_shoppable"],
                    )
                )
            except Exception:
                print(f"[agent] could not build forced tool_config for an image turn:\n{traceback.format_exc()}",
                      file=sys.stderr)
        resp = _generate_with_retry(model, contents, **call_kwargs)
        candidate = resp.candidates[0]
        parts = candidate.content.parts
        fn_calls = [] if last_turn else [p.function_call for p in parts if p.function_call]

        if not fn_calls:
            reply = "".join(p.text for p in parts if p.text).strip()
            return {"reply": reply or "I'm not sure how to help with that.", "items": items, "bundle": bundle}

        contents.append(candidate.content)
        response_parts = []
        for fc in fn_calls:
            name = fc.name
            args = dict(fc.args)
            # bundle is intentionally NOT reset here -- only the three
            # actual bundle-producing branches below (plan_office_setup,
            # plan_room_setup, match_shopping_list) assign to it. Every
            # other tool used to explicitly null it, which meant a bundle
            # from an EARLIER call in the same turn (or an earlier
            # iteration of this loop) silently lost its "Add all to cart"
            # button the moment any other tool -- even an unrelated
            # search_products, or an error path that never should have
            # touched bundle status at all -- ran afterward.
            if name == "search_products":
                result = _hybrid_search(args.get("query", ""), limit=8)
                tool_result = {"count": result["count"], "items": agent.trim_items_for_model(result["items"])}
                new_items = result["items"]
            elif name == "plan_office_setup":
                bundle = agent.plan_office_setup(args.get("people_count"), args.get("budget"))
                # get_products_by_category/get_product_by_sku (agent.py)
                # return raw catalog rows, not _serialize()'d ones -- unlike
                # search_products/match_shopping_list (which route through
                # _hybrid_search, already serialized) or complete_the_look/
                # shop_the_room (serialized explicitly below). Skipping this
                # meant every bundle card showed "You save undefined%" and
                # its image bypassed the CDN redirect.
                _serialize_bundle_items(bundle, "desk", "chair", "shared_item", "closest_desk", "closest_chair")
                tool_result = agent.trim_bundle_for_model(bundle)
                new_items = [v for k in ("desk", "chair", "shared_item", "closest_desk", "closest_chair")
                             if (v := bundle.get(k))]
            elif name == "plan_room_setup":
                bundle = agent.plan_room_setup(args.get("room_type", ""), args.get("budget"), args.get("style", ""))
                if bundle.get("items"):
                    bundle["items"] = [_serialize(p) for p in bundle["items"]]
                _serialize_bundle_items(bundle, "closest_item")
                tool_result = agent.trim_room_bundle_for_model(bundle)
                new_items = list(bundle.get("items") or [])
                if bundle.get("closest_item"):
                    new_items.append(bundle["closest_item"])
            elif name == "complete_the_look":
                sku = str(args.get("sku", "")).strip()
                matches = _do_complete_the_look(sku) if sku else None
                if matches is None:
                    tool_result = {"error": f"Unknown sku {sku!r} -- search_products first to find a real one."}
                    new_items = []
                else:
                    by_sku = get_products_by_skus([s for s, _ in matches])
                    result_items = []
                    for s, score in matches:
                        p = by_sku.get(s)
                        if p:
                            item = _serialize(p)
                            item["match_score"] = round(score * 100, 1)
                            result_items.append(item)
                    tool_result = {"count": len(result_items), "items": agent.trim_items_for_model(result_items)}
                    new_items = result_items
            elif name == "shop_the_room":
                if not image_bytes:
                    tool_result = {"error": "No photo is attached to this message. Ask the user to attach one."}
                    new_items = []
                else:
                    try:
                        matches = _do_shop_the_room(image_bytes)
                    except OSError:
                        # Same failure mode /api/shop-the-room guards against:
                        # PIL couldn't decode the attached image.
                        matches = []
                    by_sku = get_products_by_skus([sku for sku, _ in matches])
                    result_items = []
                    for sku, score in matches:
                        p = by_sku.get(sku)
                        if p:
                            item = _serialize(p)
                            item["match_score"] = round(score * 100, 1)
                            result_items.append(item)
                    tool_result = {"count": len(result_items), "items": agent.trim_items_for_model(result_items, cap=6)}
                    new_items = result_items
            elif name == "match_shopping_list":
                requested = args.get("items") or []
                matched, unmatched = [], []
                for entry in requested[:20]:   # a photographed list is still bounded -- avoid a runaway tool call
                    query = str((entry or {}).get("query", "")).strip()
                    if not query:
                        continue
                    try:
                        qty = max(1, min(int(entry.get("quantity", 1)), 999))
                    except (TypeError, ValueError):
                        qty = 1
                    top = _hybrid_search(query, limit=1)["items"]
                    if top and _plausible_match(query, top[0]):
                        item = dict(top[0])
                        item["requested_qty"] = qty
                        matched.append(item)
                    else:
                        unmatched.append(query)
                total = round(sum(item["price"] * item["requested_qty"] for item in matched), 2)
                bundle = {"feasible": bool(matched), "items": matched, "unmatched": unmatched, "total": total}
                tool_result = agent.trim_room_bundle_for_model(bundle)
                new_items = matched
            elif name == "not_shoppable":
                tool_result = {"acknowledged": True}
                new_items = []
            elif name == "swap_bundle_item":
                result = agent.swap_bundle_item(
                    args.get("sku", ""), args.get("direction", ""),
                    new_sku=args.get("new_sku"), other_skus=args.get("other_skus"))
                _serialize_bundle_items(result, "old_item", "new_item")
                if result.get("bundle", {}).get("items"):
                    result["bundle"]["items"] = [_serialize(p) for p in result["bundle"]["items"]]
                tool_result = agent.trim_swap_for_model(result)
                if result.get("feasible") and result.get("bundle"):
                    # other_skus was given -- a full reconstructed bundle (the
                    # rest of the order + the replacement), not just one card.
                    # Deliberately DOES set `bundle` here (unlike find_similar/
                    # find_deals, which leave it untouched) -- this IS the
                    # bundle-producing case, same as plan_room_setup/
                    # match_shopping_list, just assembled from context instead
                    # of built fresh.
                    bundle = result["bundle"]
                    new_items = list(bundle["items"])
                else:
                    new_items = [result["new_item"]] if result.get("feasible") else []
            elif name == "find_similar":
                sku = str(args.get("sku", "")).strip()
                refine_text = str(args.get("refine_text", "") or "").strip()
                matches = _do_similar_search(sku, 8, refine_text) if sku else None
                if matches is None:
                    tool_result = {"error": f"Unknown sku {sku!r} -- search_products first to find a real one."}
                    new_items = []
                else:
                    by_sku = get_products_by_skus([s for s, _ in matches])
                    result_items = []
                    for s, score in matches:
                        p = by_sku.get(s)
                        if p:
                            item = _serialize(p)
                            item["match_score"] = round(score * 100, 1)
                            result_items.append(item)
                    max_price = args.get("max_price")
                    if max_price is not None:
                        try:
                            max_price = float(max_price)
                            # "but under $X" -- cheapest-first within the
                            # visually-similar pool, same reasoning as the
                            # "Similar for less" rail: a straight filter left
                            # in similarity-rank order rarely felt distinct
                            # from an unfiltered list.
                            result_items = sorted(
                                [i for i in result_items if i["price"] <= max_price],
                                key=lambda i: i["price"])
                        except (TypeError, ValueError):
                            pass
                    tool_result = {"count": len(result_items), "items": agent.trim_items_for_model(result_items)}
                    new_items = result_items
            elif name == "find_deals":
                deals = agent.find_deals(args.get("category"), args.get("min_discount_pct"))
                # No "Add all to cart" bundle assigned here on purpose --
                # unlike plan_room_setup/plan_office_setup, these items
                # aren't a coherent multi-item plan, just unrelated products
                # that happen to be discounted (see the bundle-not-reset
                # comment above: leaving `bundle` untouched, not nulling it,
                # is what lets an earlier bundle from this same turn keep
                # its "Add all to cart" button).
                new_items = [_serialize(p) for p in (deals.get("items") or [])]
                tool_result = {k: v for k, v in deals.items() if k != "items"}
                tool_result["items"] = agent.trim_items_for_model(new_items)
            elif name == "compare_products":
                result = agent.compare_products(args.get("skus") or [])
                new_items = [_serialize(p) for p in result["items"]] if result["feasible"] else []
                tool_result = {"feasible": result["feasible"], "missing": result["missing"],
                                "items": agent.trim_items_for_model(new_items, cap=4)}
            else:
                tool_result = {"error": f"unknown tool {name}"}
                new_items = []
            # Accumulate + dedupe by sku rather than drop the duplicate --
            # a single turn can chain multiple search calls ("I need a desk
            # and a lamp"), and dropping silently kept whichever copy
            # happened to be seen FIRST even when a later call's copy was
            # more specific (e.g. match_shopping_list's requested_qty)
            # -- update in place instead, keeping the original card
            # position but the freshest data.
            for item in new_items:
                if item["sku"] in seen_skus:
                    for idx, existing in enumerate(items):
                        if existing["sku"] == item["sku"]:
                            items[idx] = item
                            break
                else:
                    seen_skus.add(item["sku"])
                    items.append(item)
            response_parts.append(Part.from_function_response(name=name, response=tool_result))
        contents.append(Content(role="function", parts=response_parts))

    return {
        "reply": "I looked into a few options but couldn't narrow it down — try being more specific.",
        "items": items, "bundle": bundle,
    }


_AGENT_MAX_MESSAGE_CHARS = 500
_AGENT_MAX_HISTORY_TURNS = 8   # last N turns only -- also bounds Vertex token spend per request


@app.post("/api/agent/chat")
async def agent_chat(body: AgentChatRequest):
    """Staples AI. Falls back to a plain hybrid search on the user's raw
    message if Vertex/Gemini errors or isn't configured (e.g. GCP_PROJECT
    unset locally) -- a real degraded mode, not a scripted demo path."""
    if not config.AGENT_ENABLED:
        raise HTTPException(503, "Staples AI is disabled")
    message = (body.message or "").strip()
    if len(message) > _AGENT_MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"message is too long (limit {_AGENT_MAX_MESSAGE_CHARS} characters)")

    image_bytes = None
    if body.image_b64:
        try:
            image_bytes = base64.b64decode(body.image_b64, validate=True)
        except (base64.binascii.Error, ValueError):
            raise HTTPException(400, "image_b64 is not valid base64")
        if len(image_bytes) > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")
        if not (body.image_mime or "").startswith("image/"):
            raise HTTPException(400, "image_mime must be an image/* type")

    # A bare photo with no caption is a legitimate request ("furnish this" is
    # implied) -- only reject truly empty input.
    if not message and not image_bytes:
        raise HTTPException(400, "message or image is required")

    # This is a public, unauthenticated endpoint that spends real Vertex
    # tokens per call -- an unbounded client-supplied history is an easy way
    # to balloon that cost (or just break generate_content on a request
    # that's grown too large) for a demo URL that ends up in a chat somewhere.
    history = (body.history or [])[-_AGENT_MAX_HISTORY_TURNS:]
    # 20, not the original 12 -- a receipt/list bundle (up to ~8 items) plus
    # a find_similar refinement (up to 8 more) on ONE of those items already
    # totals 13+ in a single exchange; 12 let the oldest bundle item silently
    # fall out of context before a later "replace X, keep the rest" request
    # could reference it (verified: this exact sequence lost track of an
    # item this way).
    last_items = (body.last_items or [])[:20]

    try:
        return await run_in_threadpool(
            _do_agent_chat, message, history, last_items, image_bytes, body.image_mime)
    except Exception:
        # Swallowed on purpose (falls back to plain search below) but must be
        # loud about it -- an unlogged except here means Vertex/Gemini could
        # be broken indefinitely with the demo silently degraded and no
        # signal in Cloud Logging to say why (this bit us once already: the
        # first live deploy used a Gemini model ID that 404'd, and the only
        # way to find out was a manual log dig once someone noticed the
        # replies looked generic). stderr rather than stdout so it lands at
        # ERROR severity in Cloud Logging's default stream-to-severity
        # mapping, not INFO -- the print() convention elsewhere in this file
        # is for expected startup/status lines, not failures.
        print(f"[agent] _do_agent_chat failed, falling back to hybrid search:\n{traceback.format_exc()}",
              file=sys.stderr)
        if not message:
            # Image-only request and Gemini (the only thing that can read an
            # image here) is unavailable -- plain _hybrid_search has nothing
            # to search on, so say so honestly instead of running it on "".
            return {"reply": "Staples AI is unavailable right now, and I can't read photos without it "
                              "— try describing what you're looking for in words.",
                    "items": [], "bundle": None, "degraded": True}
        result = _hybrid_search(message, limit=8)
        reply = (f"Here's what I found for \"{message}\"." if result["count"]
                 else f"I couldn't find anything for \"{message}\" — try different words.")
        return {"reply": reply, "items": result["items"], "bundle": None, "degraded": True}


# NOTE: a GCP billing hold on this project temporarily disabled several
# APIs (including cloudbuild.googleapis.com), which briefly made the whole
# service 503 independent of anything below -- unrelated to voice input
# itself, just recorded here since it looked like an app bug at first.
@app.websocket("/ws/agent/transcribe")
async def agent_transcribe_ws(ws: WebSocket, sample_rate: int = 16000):
    """Streaming voice input for the Staples AI chat's mic button -- text
    appears as the user talks, not only after they stop (see speech.py's
    module docstring for the full reasoning, including why this is raw
    LINEAR16 PCM rather than whatever MediaRecorder happens to produce).

    Protocol: client sends binary frames of raw PCM audio as they're
    captured, then a "STOP" text frame (or just disconnects -- handled the
    same way) once the user stops recording. Server sends one JSON text
    frame per partial/final transcript Google returns:
    {"text": "...", "is_final": bool}, or {"error": "..."} if the
    underlying Speech-to-Text call fails (e.g. GCP_PROJECT unset locally).
    sample_rate: the browser's own actual AudioContext sample rate --
    always correct because it's reported by the browser, never guessed
    here (see the frontend's wireAgentMic)."""
    if not config.VOICE_ENABLED:
        await ws.close(code=1008, reason="Voice input is disabled")
        return
    await ws.accept()

    chunk_queue: "queue_module.Queue" = queue_module.Queue()
    loop = asyncio.get_event_loop()

    def on_result(text, is_final):
        # Called from the worker thread below, which is NOT the asyncio
        # event loop thread -- WebSocket.send_json isn't thread-safe, so
        # hop back onto the loop rather than calling it directly here.
        asyncio.run_coroutine_threadsafe(ws.send_json({"text": text, "is_final": is_final}), loop)

    def run():
        try:
            speech.streaming_transcribe(sample_rate, chunk_queue, on_result)
        except Exception:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"error": "Voice input is unavailable right now — try typing instead."}), loop)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                chunk_queue.put(message["bytes"])
            elif message.get("text") == "STOP":
                break
    except WebSocketDisconnect:
        pass
    finally:
        chunk_queue.put(speech._END_OF_STREAM)
        # Bounded wait so a stuck/slow Speech-to-Text call can't hang this
        # handler (and the worker thread) forever -- the thread is a daemon,
        # so worst case it's abandoned, not leaked as a zombie process.
        await run_in_threadpool(worker.join, 10)


# ---------- static assets ----------

# When IMAGES_BASE_URL is set (production), redirect product-image requests
# to the GCS/Cloud CDN origin instead of serving the bytes from this
# container — registered BEFORE the static mount below so it takes
# precedence for this one path, while everything else under /images (e.g.
# the built-in 30-item demo catalog's generated PNGs, which were never
# uploaded to the CDN) still falls through to local disk. A redirect rather
# than rewriting every image_url at the source means the frontend's few
# hardcoded /images/products/... references (the homepage sample-photo
# thumbnails) get the same benefit with no frontend changes needed.
if config.IMAGES_BASE_URL:
    @app.get("/images/products/{filename}")
    def redirect_product_image(filename: str):
        # _serialize points API-served image_urls straight at the CDN now, so
        # this route only still fires for hardcoded same-origin references
        # (e.g. the homepage's hero/room sample thumbnails). Cache-Control on
        # the redirect itself lets the browser skip the round trip on repeat
        # views instead of re-hitting Cloud Run for a bare 302 every time.
        return RedirectResponse(
            f"{config.IMAGES_BASE_URL}/products/{filename}", status_code=302,
            headers={"Cache-Control": "public, max-age=86400"},
        )

app.mount("/images", StaticFiles(directory=os.path.join(BASE_DIR, "static", "images")), name="images")
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")

# Repo-root docs (GCP_SETUP.md etc.) linked from the architecture/how-it-works
# pages. Explicit allowlist rather than an open directory mount — this is a
# doc-serving convenience, not a general file server.
ROOT_DIR = os.path.dirname(BASE_DIR)
_ROOT_DOCS = {"README.md", "GCP_SETUP.md", "HOW_IT_WORKS.md", "RUN_10K.md"}


@app.get("/{doc_name}.md")
def root_doc(doc_name: str):
    fname = f"{doc_name}.md"
    path = os.path.join(ROOT_DIR, fname)
    # FileResponse doesn't check existence up front -- a missing path fails at
    # ASGI send-time as an unhandled 500, not a clean 404. Check first (also
    # covers _ROOT_DOCS drifting out of sync with what actually ships in the
    # image, e.g. Dockerfile's COPY list).
    if fname not in _ROOT_DOCS or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="text/plain; charset=utf-8")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/{page_name}.html")
def page(page_name: str):
    path = os.path.join(FRONTEND_DIR, f"{page_name}.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(404)
