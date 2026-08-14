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
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from products_data import (
    DATA_BACKEND,
    get_all_products,
    get_product_by_sku,
    get_products_by_category,
    search_products,
)
from embeddings import BACKEND as EMBEDDING_BACKEND, embed_image, top_k_matches

BASE_DIR = os.path.dirname(__file__)
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")
CACHE_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

# ---- catalog embedding cache (computed once, then persisted to disk) ----
_catalog_vectors = {}


def _image_path(p):
    fname = os.path.basename(p.get("image_url") or "") or f"{p['sku']}.png"
    return os.path.join(IMAGES_DIR, fname)


def _cache_path():
    # one cache per embedding backend (vectors differ by model/dimensionality)
    return os.path.join(CACHE_DIR, f"index_{EMBEDDING_BACKEND}.npz")


def _build_catalog_index():
    start = time.time()
    products = [p for p in get_all_products() if os.path.exists(_image_path(p))]
    want = {p["sku"] for p in products}

    # Fast path: load a prebuilt index (see build_index.py) if it covers the catalog.
    cache = _cache_path()
    if os.path.exists(cache):
        data = np.load(cache, allow_pickle=True)
        skus = list(data["skus"])
        vecs = data["vecs"]  # read the array ONCE (indexing NpzFile per-item re-reads it)
        cached = {s: vecs[i] for i, s in enumerate(skus)}
        if want.issubset(cached):
            for s in want:
                _catalog_vectors[s] = cached[s]
            print(f"[startup] backends: embedding={EMBEDDING_BACKEND}, data={DATA_BACKEND} — "
                  f"loaded {len(_catalog_vectors)} vectors from cache in {time.time() - start:.2f}s")
            return

    # Slow path: embed every catalog image, then persist for next boot.
    skus, vecs = [], []
    for p in products:
        with open(_image_path(p), "rb") as f:
            v = embed_image(f.read())
        _catalog_vectors[p["sku"]] = v
        skus.append(p["sku"])
        vecs.append(v)
    if vecs:
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.savez(cache, skus=np.array(skus, dtype=object), vecs=np.array(vecs, dtype=np.float32))
    print(f"[startup] backends: embedding={EMBEDDING_BACKEND}, data={DATA_BACKEND} — "
          f"embedded {len(_catalog_vectors)} catalog images in {time.time() - start:.2f}s (cached)")


# ---- soft category classifier (nearest-centroid over the catalog vectors) ----
# Reuses the embeddings we already cache: each category's centroid is the mean of
# its product vectors. A query is classified by its closest centroid. Used to
# *softly* scope visual search to a category when we're confident — never a hard
# gate (see visual_search: low confidence falls back to searching everything).
_sku_category = {}          # sku -> category
_category_centroids = {}    # category -> unit-norm mean vector
CONF_THRESHOLD = config.CONF_THRESHOLD   # % — only auto-scope above this confidence
_SOFTMAX_T = config.SOFTMAX_T            # temperature: sharpens cosine sims into probabilities


def _build_category_centroids():
    from collections import defaultdict
    cat_of = {p["sku"]: p["category"] for p in get_all_products()}
    groups = defaultdict(list)
    for sku, vec in _catalog_vectors.items():
        c = cat_of.get(sku)
        if not c:
            continue
        _sku_category[sku] = c
        groups[c].append(vec)
    for c, vecs in groups.items():
        m = np.mean(np.stack(vecs), axis=0)
        n = np.linalg.norm(m)
        _category_centroids[c] = (m / n) if n > 0 else m
    print(f"[startup] built {len(_category_centroids)} category centroids for the soft classifier")


def _classify(query_vec):
    """Return {category, confidence(%), ranking:[(cat,%)]} or None if no centroids."""
    if not _category_centroids:
        return None
    cats = list(_category_centroids)
    qn = np.linalg.norm(query_vec) + 1e-8
    sims = np.array([
        float(np.dot(query_vec, _category_centroids[c]) / (qn * (np.linalg.norm(_category_centroids[c]) + 1e-8)))
        for c in cats
    ])
    z = sims / _SOFTMAX_T
    z -= z.max()
    e = np.exp(z)
    probs = e / e.sum()
    order = np.argsort(probs)[::-1]
    return {
        "category": cats[order[0]],
        "confidence": round(float(probs[order[0]]) * 100, 1),
        "ranking": [(cats[i], round(float(probs[i]) * 100, 1)) for i in order[:3]],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    _build_catalog_index()
    _build_category_centroids()
    yield


app = FastAPI(title="Staples Visual Search Demo", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

@app.get("/api/products")
def list_products(category: str | None = None):
    items = get_products_by_category(category) if category else get_all_products()
    return {"count": len(items), "items": [_serialize(p) for p in items]}


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
def text_search(q: str = ""):
    items = search_products(q)
    return {"count": len(items), "query": q, "items": [_serialize(p) for p in items]}


@app.get("/api/config")
def app_config():
    """Lets the frontend show which backends are live (demo transparency)."""
    return {"embedding_backend": EMBEDDING_BACKEND, "data_backend": DATA_BACKEND}


@app.post("/api/visual-search")
async def visual_search(file: UploadFile = File(...), top_k: int = config.TOP_K, scope: str = "auto"):
    """Visual search with soft category scoping.

    scope: "auto"  (default) — scope to the predicted category only when confident
           "all"   — never scope (search the whole catalog)
           "force" — always scope to the predicted category
    """
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Please upload an image file")

    image_bytes = await file.read()
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file")

    try:
        query_vec = embed_image(image_bytes)
    except Exception as e:
        raise HTTPException(400, f"Could not process image: {e}")

    # classify the uploaded photo, then decide whether to scope the search
    cls = _classify(query_vec) if config.CLASSIFIER_ENABLED else None
    catalog = list(_catalog_vectors.items())
    scoped = False
    if cls:
        confident = cls["confidence"] >= CONF_THRESHOLD
        if scope == "force" or (scope == "auto" and confident):
            subset = [(s, v) for s, v in catalog if _sku_category.get(s) == cls["category"]]
            if subset:
                catalog = subset
                scoped = True

    matches = top_k_matches(query_vec, catalog, k=top_k)

    results = []
    for sku, score in matches:
        p = get_product_by_sku(sku)
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
        "scoped": scoped,
        "searched": len(catalog),  # how many vectors were actually ranked
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
