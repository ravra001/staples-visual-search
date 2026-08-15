"""
Cloud SQL (Postgres + pgvector) repository — the DATA_BACKEND=sql implementation.

Mirrors the in-memory repository's CRUD functions exactly, so main.py's
product lookups are unchanged when you switch backends. What's genuinely
different under this backend is where VECTOR SEARCH happens: instead of an
in-memory NumPy matrix (main.py's _rank/_display_scores), similarity is
computed *inside Postgres* via pgvector's `<=>` cosine-distance operator,
index-accelerated by an HNSW index on the `embedding` column. See
search_by_vector() below — main.py calls it directly when DATA_BACKEND=sql,
bypassing the in-memory index entirely.

Setup — local Postgres with pgvector (see docker-compose.yml at the repo
root), or Cloud SQL for Postgres with the pgvector extension enabled:

    docker compose up -d                 # local pgvector, one command
    pip install -r requirements-ml.txt   # SQLAlchemy + psycopg + pgvector
    export DATABASE_URL=postgresql+psycopg://staples:staples@localhost:5432/staples
    # or via config.yaml: data.backend: sql, data.database_url: ...

Then seed it once, reusing the vectors your in-memory index has ALREADY
computed (fast — no re-embedding) — see build_index.py for how that index
is built:

    cd backend
    python -c "import products_repo_sql as r; r.init_and_seed(reuse_vectors_from='data/index_clip.npz')"

Then run the app with DATA_BACKEND=sql. main.py's visual-search endpoint will
route to search_by_vector() instead of the in-memory matrix.
"""
import os

import numpy as np
from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, String, Text, create_engine,
    func, or_, select, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from pgvector.sqlalchemy import Vector

import config
from embeddings import embedding_dim

DATABASE_URL = config.DATABASE_URL
if not DATABASE_URL:
    raise RuntimeError("data.backend=sql requires data.database_url in config.yaml (or DATABASE_URL).")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)
Base = declarative_base()

EMBED_DIM = embedding_dim()  # fixed at import time — pgvector needs a declared width


class Product(Base):
    __tablename__ = "products"

    sku = Column(String(32), primary_key=True)
    name = Column(String(255), nullable=False)
    brand = Column(String(80), default="")
    category = Column(String(40), index=True, nullable=False)
    price = Column(Float, nullable=False)
    list_price = Column(Float, nullable=False)
    rating = Column(Float, default=0.0)
    reviews = Column(Integer, default=0)
    description = Column(Text, default="")
    image_url = Column(String(255), default="")

    # RANKING vector (image+text fused when text_fusion is enabled — same
    # recipe as the in-memory backend's embed_catalog_item()). Indexed below.
    embedding = Column(Vector(EMBED_DIM))
    # DISPLAY-only vector (pure image) — deliberately UNINDEXED. Nothing ever
    # orders by this column; it's read only for the already-ranked top-k, the
    # same "rank by fused, show pure-image" split main.py does in memory (see
    # search_by_vector — both scores come out of ONE query, no second round trip).
    image_embedding = Column(Vector(EMBED_DIM))
    # Identity of the model (+fusion config+catalog content) both vectors
    # above were built with — the SQL analogue of main.py's index fingerprint.
    # A row whose model_version doesn't match the live config is stale and
    # should be re-embedded, not ranked against.
    model_version = Column(String(160))
    embedded_at = Column(DateTime(timezone=True), server_default=func.now())

    def as_dict(self):
        return {
            "sku": self.sku, "name": self.name, "brand": self.brand,
            "category": self.category, "price": self.price, "list_price": self.list_price,
            "rating": self.rating, "reviews": self.reviews, "description": self.description,
            "image_url": self.image_url,
        }


# HNSW index on the FUSED vector only — that's the only column anything ever
# orders by. cosine distance ops to match .cosine_distance() below (pgvector
# requires the index's operator class to match the distance function used at
# query time, or the index is silently not used).
Index(
    "ix_products_embedding_hnsw", Product.embedding,
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_ops={"embedding": "vector_cosine_ops"},
)


class CategoryCentroid(Base):
    """One row per category — the mean of that category's FUSED vectors,
    precomputed at seed time so the soft classifier's cold start is a 13-row
    SELECT instead of scanning the whole products table on every boot."""
    __tablename__ = "category_centroids"

    category = Column(String(40), primary_key=True)
    centroid = Column(Vector(EMBED_DIM))
    model_version = Column(String(160))


# --------------------------------------------------------------------------
# CRUD — mirrors the in-memory repository's interface exactly
# --------------------------------------------------------------------------

def get_all_products():
    with SessionLocal() as s:
        return [p.as_dict() for p in s.query(Product).all()]


def get_product_by_sku(sku):
    with SessionLocal() as s:
        p = s.get(Product, sku)
        return p.as_dict() if p else None


def get_products_by_skus(skus):
    """Batch fetch — one `WHERE sku IN (...)` query (avoids N+1). {sku: product}."""
    skus = list(skus)
    if not skus:
        return {}
    with SessionLocal() as s:
        rows = s.query(Product).filter(Product.sku.in_(skus)).all()
        return {p.sku: p.as_dict() for p in rows}


def get_products_by_category(category):
    with SessionLocal() as s:
        rows = s.query(Product).filter(Product.category == category).all()
        return [p.as_dict() for p in rows]


def search_products(query):
    q = (query or "").strip()
    if not q:
        return []
    with SessionLocal() as s:
        # AND across terms, each matched against name/brand/description.
        stmt = s.query(Product)
        for term in q.lower().split():
            like = f"%{term}%"
            stmt = stmt.filter(or_(
                func.lower(Product.name).like(like),
                func.lower(Product.brand).like(like),
                func.lower(Product.category).like(like),
                func.lower(Product.description).like(like),
            ))
        return [p.as_dict() for p in stmt.all()]


# --------------------------------------------------------------------------
# Vector search — this is what DATA_BACKEND=sql actually changes vs. memory
# --------------------------------------------------------------------------

def search_by_vector(query_vec, category=None, k=8):
    """pgvector similarity search. RANKS by the fused `embedding` column
    (HNSW-indexed) and, for the SAME already-ranked rows, also returns a
    pure-image `display_score` from `image_embedding` — one query computes
    both, mirroring main.py's in-memory dual-score design (rank by fused,
    display pure-image so a genuine match reads ~85-100% instead of the
    compressed ~60% a fused-vs-fused-style score would show).

    Returns [(sku, rank_score, display_score), ...] sorted by rank_score desc.
    query_vec: a 1-D array/list, pure-image (the query is always pure-image —
    a user only supplies a photo, never text).
    """
    with SessionLocal() as s:
        rank_dist = Product.embedding.cosine_distance(query_vec)
        disp_dist = Product.image_embedding.cosine_distance(query_vec)
        stmt = (
            select(Product.sku, (1 - rank_dist).label("rank_score"), (1 - disp_dist).label("display_score"))
            .order_by(rank_dist)   # ASC on distance == descending similarity; must stay ASC to use the HNSW index
            .limit(k)
        )
        if category is not None:
            stmt = stmt.where(Product.category == category)
        rows = s.execute(stmt).all()
        return [(r.sku, float(r.rank_score), float(r.display_score)) for r in rows]


def count_by_category(category=None):
    """Used for the "searched" field (how many rows were actually eligible) —
    the SQL analogue of counting a category mask over the in-memory matrix."""
    with SessionLocal() as s:
        stmt = select(func.count()).select_from(Product)
        if category is not None:
            stmt = stmt.where(Product.category == category)
        return int(s.execute(stmt).scalar() or 0)


def get_category_centroids():
    """{category: np.ndarray} for the soft classifier — a 13-row SELECT, not
    a scan of the whole products table. Call once at server startup."""
    with SessionLocal() as s:
        rows = s.query(CategoryCentroid).all()
        return {r.category: np.array(r.centroid, dtype=np.float32) for r in rows}


def refresh_category_centroids(model_version):
    """Recompute + upsert one centroid per category (mean of that category's
    FUSED vectors, L2-normalized) — matches main.py's in-memory
    _build_category_centroids(). Call after (re)seeding. Computed in Python
    rather than SQL avg(vector) for portability across pgvector versions
    (avg(vector) needs pgvector >= 0.7)."""
    with SessionLocal() as s:
        cats = [r[0] for r in s.query(Product.category).distinct().all()]
        for cat in cats:
            rows = s.query(Product.embedding).filter(Product.category == cat).all()
            vecs = np.array([r[0] for r in rows], dtype=np.float32)
            if vecs.size == 0:
                continue
            centroid = vecs.mean(axis=0)
            n = np.linalg.norm(centroid)
            if n > 0:
                centroid = centroid / n
            existing = s.get(CategoryCentroid, cat)
            if existing:
                existing.centroid, existing.model_version = centroid, model_version
            else:
                s.add(CategoryCentroid(category=cat, centroid=centroid, model_version=model_version))
        s.commit()
        print(f"[sql] refreshed {len(cats)} category centroids")


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def _image_path(p):
    fname = os.path.basename(p.get("image_url") or "") or f"{p['sku']}.png"
    return os.path.join(IMAGES_DIR, fname)


def _load_reusable_vectors(path):
    """sku -> (fused_vec, image_vec) from an already-built index_*.npz (see
    build_index.py). Avoids re-embedding the whole catalog just to seed
    Postgres with vectors the in-memory pipeline already computed."""
    if not path or not os.path.exists(path):
        return {}
    d = np.load(path, allow_pickle=True)
    skus = list(d["skus"])
    fused = d["vecs"]
    image = d["image_vecs"] if "image_vecs" in d else fused  # fall back to fused if no dual storage
    return {s: (fused[i], image[i]) for i, s in enumerate(skus)}


def init_and_seed(reuse_vectors_from=None, embed_missing=False):
    """Create the schema (+ pgvector extension + HNSW index) and seed it from
    the currently loaded catalog (products_data.PRODUCTS — whatever
    config.yaml's data.catalog_file points to; independent of DATA_BACKEND,
    so this never seeds from itself).

    reuse_vectors_from: path to an index_*.npz to reuse vectors from (fast,
        recommended — see build_index.py). Products not covered by it are
        skipped unless embed_missing=True.
    embed_missing: embed any uncovered product on the fly (slow — one model
        call per product). Off by default so seeding never silently triggers
        a long embedding pass.
    """
    from products_data import PRODUCTS, catalog_content_hash  # the loaded catalog, not this module's own table

    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)

    reusable = _load_reusable_vectors(reuse_vectors_from)
    model_version = config.index_fingerprint(catalog_hash=catalog_content_hash())

    if embed_missing:
        from embeddings import embed_catalog_item

    seeded, skipped = 0, []
    with SessionLocal() as s:
        for p in PRODUCTS:
            sku = p["sku"]
            vecs = reusable.get(sku)
            if vecs is None:
                if not embed_missing:
                    skipped.append(sku)
                    continue
                img_path = _image_path(p)
                if not os.path.exists(img_path):
                    skipped.append(sku)
                    continue
                with open(img_path, "rb") as f:
                    fused, image_vec = embed_catalog_item(
                        f.read(), name=p.get("name", ""), brand=p.get("brand", ""),
                        description=p.get("description", ""), return_image_vec=True)
                vecs = (fused, image_vec)

            fused_vec, image_vec = vecs
            row = s.get(Product, sku) or Product(sku=sku)
            row.name = p.get("name", "")
            row.brand = p.get("brand", "")
            row.category = p["category"]
            row.price = p.get("price", 0.0)
            row.list_price = p.get("list_price", p.get("price", 0.0))
            row.rating = p.get("rating", 0.0)
            row.reviews = p.get("reviews", 0)
            row.description = p.get("description", "")
            row.image_url = p.get("image_url") or f"/images/products/{sku}.png"
            row.embedding = np.asarray(fused_vec, dtype=np.float32)
            row.image_embedding = np.asarray(image_vec, dtype=np.float32)
            row.model_version = model_version
            s.add(row)
            seeded += 1
        s.commit()

    if skipped:
        print(f"[sql] WARNING: {len(skipped)} product(s) had no vector available and were skipped "
              f"(pass embed_missing=True, or point reuse_vectors_from at an index covering the full catalog): "
              f"{skipped[:5]}{'...' if len(skipped) > 5 else ''}")
    print(f"[sql] seeded {seeded} products ({model_version}) into {engine.url.database}")

    refresh_category_centroids(model_version)
