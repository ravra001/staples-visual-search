"""
EXPERIMENT — embed every view of every product and save a MULTI-VECTOR index:
one row per (sku, view). Reuses the production embed_image() (same CLIP model),
so the vectors are directly comparable to the main catalog.

Usage (from backend/):
    python experimental/build_multiview_index.py

Output: data/index_multiview.npz  { row_skus, row_views, vecs, fingerprint }
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from embeddings import embed_image

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(BACKEND, "static", "images", "multiview")
CATALOG = os.path.join(BACKEND, "data", "catalog_multiview.json")
OUT = os.path.join(BACKEND, "data", "index_multiview.npz")


def main():
    with open(CATALOG, encoding="utf-8") as f:
        products = json.load(f)

    row_skus, row_views, vecs = [], [], []
    total = sum(len(p["views"]) for p in products)
    start = time.time()
    done = 0
    for p in products:
        for i, fname in enumerate(p["views"]):
            path = os.path.join(IMG_DIR, fname)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                vecs.append(embed_image(fh.read()))
            row_skus.append(p["sku"])
            row_views.append(i)          # 0 = main image
            done += 1
            if done % 500 == 0:
                print(f"[index] {done}/{total} views embedded ({done/(time.time()-start):.0f}/s)")

    np.savez(
        OUT,
        row_skus=np.array(row_skus, dtype=object),
        row_views=np.array(row_views, dtype=np.int16),
        vecs=np.array(vecs, dtype=np.float32),
        fingerprint=np.array([config.index_fingerprint()], dtype=object),
    )
    print(f"[index] {len(row_skus)} view-vectors from {len(products)} products "
          f"({config.index_fingerprint()}) -> {OUT} in {time.time()-start:.1f}s")


if __name__ == "__main__":
    main()
