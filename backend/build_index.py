"""
Precompute the catalog embedding index and cache it to disk, so the server
starts instantly even with a large (10k) catalog.

Respects the same config as the app (config.yaml, or env var overrides):
    EMBEDDING_BACKEND = heuristic | clip | vertex   (which model)
    CATALOG_FILE      = data/catalog_abo.json       (which catalog)

Catalog vectors are built via embed_catalog_item(), which fuses each product's
image with its name/brand/description text when embedding.text_fusion.enabled
is set (clip backend only) — validated to improve retrieval, see
backend/experimental/eval_text_fusion.py. The query side is unaffected (a user
only supplies a photo; visual_search() always uses plain embed_image()).

Writes: data/index_<EMBEDDING_BACKEND>.npz  ({skus, vecs, fingerprint}).

Usage (from backend/):
    python build_index.py
    # or, to reuse already-embedded IMAGE vectors and only (re)compute text
    # fusion (fast — skips the slow image-embedding pass entirely):
    python build_index.py --reuse-vectors-from data/index_clip.npz
"""
import argparse
import os
import time

import numpy as np

import config
from products_data import get_all_products
from embeddings import BACKEND, embed_catalog_item, embed_text_clip, _l2norm

BASE_DIR = os.path.dirname(__file__)
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")
CACHE_DIR = os.path.join(BASE_DIR, "data")


def _image_path(p):
    fname = os.path.basename(p.get("image_url") or "") or f"{p['sku']}.png"
    return os.path.join(IMAGES_DIR, fname)


def _load_reusable_vectors(path):
    if not path or not os.path.exists(path):
        return {}
    d = np.load(path, allow_pickle=True)
    vecs = {s: v for s, v in zip(d["skus"], d["vecs"])}
    print(f"[build_index] reusing {len(vecs)} cached image vectors from {path} (skips re-embedding images)")
    return vecs


def main(reuse_vectors_from=None):
    products = [p for p in get_all_products() if os.path.exists(_image_path(p))]
    total = len(products)
    print(f"[build_index] backend={BACKEND} · {total} images to embed "
          f"(text_fusion={'on' if config.TEXT_FUSION_ENABLED else 'off'})")

    reusable = _load_reusable_vectors(reuse_vectors_from)

    skus, vecs = [], []
    start = time.time()
    for i, p in enumerate(products, 1):
        sku = p["sku"]
        name, brand, desc = p.get("name", ""), p.get("brand", ""), p.get("description", "")
        if sku in reusable and config.TEXT_FUSION_ENABLED and BACKEND == "clip":
            # Fast path: reuse the cached pure-image vector, only embed+fuse text.
            text = f"{name}. {brand}. {desc}".strip(". ")
            if text.strip(". "):
                tvec = embed_text_clip(text[:300])
                w = config.TEXT_FUSION_IMAGE_WEIGHT
                v = _l2norm(w * _l2norm(reusable[sku]) + (1 - w) * _l2norm(tvec))
            else:
                v = reusable[sku]
        else:
            with open(_image_path(p), "rb") as f:
                v = embed_catalog_item(f.read(), name=name, brand=brand, description=desc)
        vecs.append(v)
        skus.append(sku)
        if i % 500 == 0:
            rate = i / (time.time() - start)
            eta = (total - i) / rate if rate else 0
            print(f"[build_index] {i}/{total}  ({rate:.0f}/s, ETA {eta:.0f}s)")

    os.makedirs(CACHE_DIR, exist_ok=True)
    out = os.path.join(CACHE_DIR, f"index_{BACKEND}.npz")
    np.savez(out, skus=np.array(skus, dtype=object), vecs=np.array(vecs, dtype=np.float32),
             fingerprint=np.array([config.index_fingerprint()], dtype=object))
    print(f"[build_index] wrote {len(skus)} vectors ({config.index_fingerprint()}) -> {out} in {time.time() - start:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-vectors-from", default=None,
                     help="existing index npz to reuse pure-image vectors from (fast text-fusion-only rebuild)")
    main(ap.parse_args().reuse_vectors_from)
