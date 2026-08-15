"""
EXPERIMENT — precompute the vectors needed to compare three retrieval methods
LIVE, side by side, on the same 500-product subset used in eval_recall.py /
eval_text_fusion.py:

  baseline    single main image per product (today's pre-fusion design)
  multiview   every view per product, ranked with max-score-over-views
              (TESTED NEGATIVE — see eval_recall.py: recall@1 -9.3pt)
  fusion      single main image + text (name/brand/description), fused
              (TESTED POSITIVE — this is what production actually uses)

Reuses the already-embedded view vectors in index_multiview.npz (no
re-embedding images) — only computes the ~500 text embeddings needed for the
fusion column.

Usage (from backend/):  python experimental/build_compare_index.py
Output: data/index_compare.npz  {skus, single_vecs, fused_vecs}
        (the multiview column is served directly from index_multiview.npz)
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from embeddings import embed_text_clip, _l2norm

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MV_INDEX = os.path.join(BACKEND, "data", "index_multiview.npz")
CATALOG = os.path.join(BACKEND, "data", "catalog_multiview_text.json")
OUT = os.path.join(BACKEND, "data", "index_compare.npz")


def main():
    d = np.load(MV_INDEX, allow_pickle=True)
    row_skus = d["row_skus"]
    row_views = d["row_views"].astype(int)
    vecs = d["vecs"]

    # main-image (view 0) vector per sku, reused as-is — this IS the baseline.
    main_vec_of = {}
    for i, (s, v) in enumerate(zip(row_skus, row_views)):
        if v == 0:
            main_vec_of[str(s)] = vecs[i]

    with open(CATALOG, encoding="utf-8") as f:
        products = {p["sku"]: p for p in json.load(f)}

    skus, single_vecs, fused_vecs = [], [], []
    w = config.TEXT_FUSION_IMAGE_WEIGHT
    for i, (sku, p) in enumerate(products.items(), 1):
        if sku not in main_vec_of:
            continue
        image_vec = main_vec_of[sku]
        text = f"{p.get('name', '')}. {p.get('brand', '')}. {p.get('description', '')}"[:300].strip(". ")
        if text.strip(". "):
            tvec = embed_text_clip(text)
            fused = _l2norm(w * _l2norm(image_vec) + (1 - w) * _l2norm(tvec))
        else:
            fused = image_vec
        skus.append(sku)
        single_vecs.append(image_vec)
        fused_vecs.append(fused)
        if i % 100 == 0:
            print(f"[build_compare_index] {i}/{len(products)}")

    np.savez(OUT, skus=np.array(skus, dtype=object),
              single_vecs=np.array(single_vecs, dtype=np.float32),
              fused_vecs=np.array(fused_vecs, dtype=np.float32))
    print(f"[build_compare_index] wrote {len(skus)} products -> {OUT}")


if __name__ == "__main__":
    main()
