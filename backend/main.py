"""
Staples-style visual search demo — FastAPI backend.

In-memory catalog + brute-force cosine similarity for now. See
products_data.py and embeddings.py for the two intended swap points:
  - products_data.py -> Cloud SQL (Postgres) repository
  - embeddings.py     -> Vertex AI Multimodal Embeddings + (optionally)
                          Vertex AI Vector Search, once the catalog is large
                          enough that brute force stops being instant.
"""
import os
import time
from contextlib import asynccontextmanager

import numpy as np
import config
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from products_data import (
    DATA_BACKEND,
    get_all_products,
    get_product_by_sku,
    get_products_by_skus,
    get_products_by_category,
    search_products,
)
from embeddings import BACKEND as EMBEDDING_BACKEND, embed_image

BASE_DIR = os.path.dirname(__file__)
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")
CACHE_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

# ---- catalog vector index (loaded once at startup) ----
# The index is held as ONE contiguous, L2-normalized matrix + parallel arrays,
# so a query ranks the whole catalog with a single matmul (see _rank) instead of
# a Python loop. Vectors are normalized once here, never per request.
_index_skus = np.empty(0, dtype=object)   # (N,)  sku per row
_index_matrix = np.empty((0, 0), np.float32)  # (N, D) L2-normalized
_index_cats = np.empty(0, dtype=object)   # (N,)  category per row
_sku_category = {}                        # sku -> category
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
    global _index_skus, _index_matrix, _index_cats
    start = time.time()
    products = [p for p in get_all_products() if os.path.exists(_image_path(p))]
    want = {p["sku"] for p in products}
    cat_of = {p["sku"]: p["category"] for p in get_all_products()}
    fp = config.index_fingerprint()

    skus = vecs = None
    cache = _cache_path()
    if os.path.exists(cache):
        data = np.load(cache, allow_pickle=True)
        cached_fp = str(data["fingerprint"][0]) if "fingerprint" in data else None
        cached_skus = list(data["skus"])
        if cached_fp is not None and cached_fp != fp:
            # A model/pretrained change since this cache was built — refuse it
            # rather than rank against vectors from a different model.
            print(f"[startup] WARNING: index cache {os.path.basename(cache)} was built by "
                  f"{cached_fp!r}, but this process is {fp!r}. Ignoring stale cache.")
        elif want.issubset(set(cached_skus)):
            skus, vecs = cached_skus, data["vecs"]
            print(f"[startup] loaded {len(skus)} vectors from cache ({fp}) in {time.time() - start:.2f}s")

    if skus is None:
        # Cache miss / stale / partial. In production this must not happen — a
        # cold start firing thousands of embed calls is a cost/latency trap.
        if config.REQUIRE_PREBUILT_INDEX:
            raise RuntimeError(
                f"No valid prebuilt index for {fp} at {cache} and index.require_prebuilt=true. "
                f"Build it offline with build_index.py and ship it."
            )
        skus, out = [], []
        for p in products:
            with open(_image_path(p), "rb") as f:
                out.append(embed_image(f.read()))
            skus.append(p["sku"])
        vecs = np.array(out, dtype=np.float32) if out else np.empty((0, 0), np.float32)
        if len(skus):
            os.makedirs(CACHE_DIR, exist_ok=True)
            np.savez(cache, skus=np.array(skus, dtype=object), vecs=vecs,
                     fingerprint=np.array([fp], dtype=object))
        print(f"[startup] embedded {len(skus)} images ({fp}) in {time.time() - start:.2f}s (cached)")

    _index_skus = np.array(skus, dtype=object)
    _index_matrix = _normalize_rows(vecs) if len(skus) else np.empty((0, 0), np.float32)
    _index_cats = np.array([cat_of.get(s, "") for s in skus], dtype=object)
    _sku_category.update({s: cat_of.get(s, "") for s in skus})
    print(f"[startup] backends: embedding={EMBEDDING_BACKEND}, data={DATA_BACKEND} — "
          f"index ready: {_index_matrix.shape}")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _build_catalog_index()
    _build_category_centroids()
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
    return {
        **p,
        "image_url": p.get("image_url") or f"/images/products/{p['sku']}.png",
        "savings_pct": max(savings, 0),
    }


# ---------- API ----------

def _page(items, limit, offset):
    """Slice a result list into a page and report the true total."""
    total = len(items)
    offset = max(offset, 0)
    window = items[offset: offset + limit] if limit is not None else items[offset:]
    return total, window


@app.get("/api/products")
def list_products(category: str | None = None, limit: int | None = config.PAGE_SIZE, offset: int = 0):
    items = get_products_by_category(category) if category else get_all_products()
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
    cats = sorted({p["category"] for p in get_all_products()})
    return {"categories": cats}


@app.get("/api/search")
def text_search(q: str = "", limit: int | None = config.PAGE_SIZE, offset: int = 0):
    items = search_products(q)
    total, window = _page(items, limit, offset)
    return {"count": total, "query": q, "offset": offset, "limit": limit,
            "items": [_serialize(p) for p in window]}


@app.get("/api/config")
def app_config():
    """Lets the frontend show which backends are live (demo transparency)."""
    return {"embedding_backend": EMBEDDING_BACKEND, "data_backend": DATA_BACKEND}


_MAX_UPLOAD_BYTES = int(config.MAX_UPLOAD_MB * 1024 * 1024)


def _do_visual_search(image_bytes, top_k, scope):
    """CPU-bound work — embed + classify + rank. Runs in a threadpool so it never
    blocks the event loop (see the endpoint)."""
    query_vec = embed_image(image_bytes)
    qn = query_vec / (np.linalg.norm(query_vec) + 1e-8)   # normalize the query ONCE

    cls = _classify(qn) if config.CLASSIFIER_ENABLED else None
    category, scoped = None, False
    if cls and (scope == "force" or (scope == "auto" and cls["confidence"] >= CONF_THRESHOLD)):
        category, scoped = cls["category"], True

    matches = _rank(qn, top_k, category=category)
    if scoped and not matches:                # empty scoped result → fall back to full search
        category, scoped = None, False
        matches = _rank(qn, top_k)

    searched = int((_index_cats == category).sum()) if category is not None else int(_index_skus.shape[0])
    return {"cls": cls, "scoped": scoped, "matches": matches, "searched": searched}


@app.post("/api/visual-search")
async def visual_search(request: Request, file: UploadFile = File(...),
                        top_k: int = config.TOP_K, scope: str = "auto"):
    """Visual search with soft category scoping.

    scope: "auto"  (default) — scope to the predicted category only when confident
           "all"   — never scope (search the whole catalog)
           "force" — always scope to the predicted category
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Please upload an image file")

    # Reject clearly-oversized uploads up front (before reading the body).
    clen = request.headers.get("content-length")
    if clen and int(clen) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")

    image_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)   # bounded read (backstop; don't buffer huge uploads)
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file")
    if len(image_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Image too large (limit {config.MAX_UPLOAD_MB:g} MB)")

    try:
        r = await run_in_threadpool(_do_visual_search, image_bytes, top_k, scope)
    except Exception as e:
        raise HTTPException(400, f"Could not process image: {e}")

    cls, matches = r["cls"], r["matches"]
    by_sku = get_products_by_skus([sku for sku, _ in matches])   # single batch fetch (no N+1)
    results = []
    for sku, score in matches:
        p = by_sku.get(sku)
        if p:
            item = _serialize(p)
            item["match_score"] = round(score * 100, 1)
            results.append(item)

    return {
        "count": len(results),
        "items": results,
        "predicted_category": cls["category"] if cls else None,
        "confidence": cls["confidence"] if cls else None,
        "category_ranking": cls["ranking"] if cls else None,
        "scoped": r["scoped"],
        "searched": r["searched"],   # how many vectors were actually ranked
        "match_threshold": config.MATCH_THRESHOLD,   # results below this % are "weak"
    }


# ---------- static assets ----------

app.mount("/images", StaticFiles(directory=os.path.join(BASE_DIR, "static", "images")), name="images")
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/{page_name}.html")
def page(page_name: str):
    path = os.path.join(FRONTEND_DIR, f"{page_name}.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(404)


app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
