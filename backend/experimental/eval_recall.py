"""
EXPERIMENT — does indexing multiple angles per product improve retrieval?

Protocol (held-out-angle recall@k):
  For every product, hold out ONE non-main view as the QUERY. Then rank the
  gallery and check whether the correct product appears in the top-k (products
  are scored by the MAX cosine over their view-vectors — "any angle matches").

Two galleries, same queries:
  single  — gallery = each product's MAIN image only (today's design)
  multi   — gallery = all of each product's views EXCEPT its held-out query

If multi-view helps, its recall@k should beat single-view — because an off-angle
query can now match one of the product's other stored angles.

Usage (from backend/):  python experimental/eval_recall.py
"""
import os
import sys
from collections import defaultdict

import numpy as np

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDX = os.path.join(BACKEND, "data", "index_multiview.npz")
KS = (1, 5, 8)


def _norm(m):
    m = np.asarray(m, np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def recall(query_rows, gallery_rows, mat, row_skus, ks):
    """For each query row, rank gallery rows by cosine, dedupe to best-per-sku,
    and record whether the query's true sku lands in the top-k."""
    G = mat[gallery_rows]                     # (g, d) normalized
    g_skus = row_skus[gallery_rows]
    hits = {k: 0 for k in ks}
    for qr in query_rows:
        q = mat[qr]
        scores = G @ q                        # cosine (all normalized)
        best = {}
        for s, sc in zip(g_skus, scores):     # max score per sku
            if sc > best.get(s, -1e9):
                best[s] = sc
        ranked = sorted(best, key=best.get, reverse=True)
        true = row_skus[qr]
        for k in ks:
            if true in ranked[:k]:
                hits[k] += 1
    n = len(query_rows)
    return {k: hits[k] / n for k in ks}, n


def main():
    d = np.load(IDX, allow_pickle=True)
    row_skus = d["row_skus"]
    row_views = d["row_views"].astype(int)
    mat = _norm(d["vecs"])

    # rows grouped by sku
    by_sku = defaultdict(list)
    for i, s in enumerate(row_skus):
        by_sku[s].append(i)

    # per product: query = a held-out NON-main view (highest view index);
    # need at least main(0) + one other to form a query.
    query_rows, single_gallery, multi_exclude = [], [], set()
    products_used = 0
    for s, rows in by_sku.items():
        rows = sorted(rows, key=lambda r: row_views[r])
        others = [r for r in rows if row_views[r] != 0]
        main = [r for r in rows if row_views[r] == 0]
        if not others or not main:
            continue
        q = others[-1]                        # held-out query angle
        query_rows.append(q)
        multi_exclude.add(q)
        single_gallery.append(main[0])        # this product's main image
        products_used += 1

    all_rows = np.arange(len(row_skus))
    multi_gallery = np.array([r for r in all_rows if r not in multi_exclude])
    single_gallery = np.array(single_gallery)
    query_rows = np.array(query_rows)

    single, n = recall(query_rows, single_gallery, mat, row_skus, KS)
    multi, _ = recall(query_rows, multi_gallery, mat, row_skus, KS)

    print(f"\nHeld-out-angle recall  —  {products_used} products, {len(query_rows)} off-angle queries")
    print(f"gallery rows: single={len(single_gallery)} (main only)   multi={len(multi_gallery)} (all views minus held-out)\n")
    print(f"  {'metric':<12}{'single-vector':>16}{'multi-vector':>16}{'lift':>10}")
    for k in KS:
        s, m = single[k] * 100, multi[k] * 100
        print(f"  recall@{k:<6}{s:>14.1f}%{m:>15.1f}%{m - s:>+9.1f}")
    print()


if __name__ == "__main__":
    main()
