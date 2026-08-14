"""
Precompute the catalog embedding index and cache it to disk, so the server
starts instantly even with a large (10k) catalog.

Respects the same env vars as the app:
    EMBEDDING_BACKEND = heuristic | clip | vertex   (which model)
    CATALOG_FILE      = data/catalog_abo.json       (which catalog)

Writes: data/index_<EMBEDDING_BACKEND>.npz  ({skus, vecs}).

Usage (from backend/):
    EMBEDDING_BACKEND=clip CATALOG_FILE=data/catalog_abo.json python build_index.py
"""
import os
import time

import numpy as np

import config
from products_data import get_all_products
from embeddings import BACKEND, embed_image

BASE_DIR = os.path.dirname(__file__)
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")
CACHE_DIR = os.path.join(BASE_DIR, "data")


def _image_path(p):
    fname = os.path.basename(p.get("image_url") or "") or f"{p['sku']}.png"
    return os.path.join(IMAGES_DIR, fname)


def main():
    products = [p for p in get_all_products() if os.path.exists(_image_path(p))]
    total = len(products)
    print(f"[build_index] backend={BACKEND} · {total} images to embed")

    skus, vecs = [], []
    start = time.time()
    for i, p in enumerate(products, 1):
        with open(_image_path(p), "rb") as f:
            vecs.append(embed_image(f.read()))
        skus.append(p["sku"])
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
    main()
