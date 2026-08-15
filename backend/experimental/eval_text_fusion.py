"""
EXPERIMENT — does fusing catalog image+text embeddings improve retrieval, vs.
image-only? (Amazon's published pattern: catalog vectors = fused image+text;
the query stays pure image, since a user only uploads a photo.)

Protocol — identical query set to eval_recall.py (held-out-angle recall@k), so
this result is directly comparable to the multi-view experiment:
  For each product, hold out one non-main view as the QUERY (pure image).
  Gallery = ONE vector per product (matching production: one image per SKU):
    image-only  — the product's main-image CLIP vector (today's design)
    fused       — normalize(alpha * image_vec + (1-alpha) * text_vec)
                  text_vec = CLIP text-tower embedding of "name. brand. description"

Reuses backend/data/index_multiview.npz for the image vectors + query rows
(no re-embedding of images — only the ~500 short text strings are embedded here).

Usage (from backend/):  python experimental/eval_text_fusion.py
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
import embeddings  # reuse the already-loaded CLIP model/device (no duplicate load)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDX = os.path.join(BACKEND, "data", "index_multiview.npz")
CATALOG = os.path.join(BACKEND, "data", "catalog_multiview_text.json")
KS = (1, 5, 8)
ALPHAS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)   # image-vs-text weight to try, image_weight = alpha


def _norm(v):
    v = np.asarray(v, np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _norm_rows(m):
    m = np.asarray(m, np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def embed_text(text):
    """CLIP text-tower embedding, L2-normalized. Reuses embeddings._get_clip()."""
    import open_clip
    s = embeddings._get_clip()
    torch = s["torch"]
    tokenizer = open_clip.get_tokenizer(config.CLIP_MODEL)
    tokens = tokenizer([text]).to(s["device"])
    with torch.no_grad():
        feats = s["model"].encode_text(tokens)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().numpy().astype(np.float32)[0]


def recall(query_rows, gallery_vecs, gallery_skus, mat, row_skus, ks):
    G = _norm_rows(gallery_vecs)          # (products, d) — one row per SKU already
    hits = {k: 0 for k in ks}
    for qr in query_rows:
        q = mat[qr]
        scores = G @ q
        ranked = gallery_skus[np.argsort(-scores)]
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
    mat = _norm_rows(d["vecs"])

    with open(CATALOG, encoding="utf-8") as f:
        products = {p["sku"]: p for p in json.load(f)}

    by_sku = defaultdict(list)
    for i, s in enumerate(row_skus):
        by_sku[s].append(i)

    # Same held-out-query construction as eval_recall.py: hold out the last
    # (highest-index) non-main view per product as the query.
    query_rows, main_row_of = [], {}
    for s, rows in by_sku.items():
        rows = sorted(rows, key=lambda r: row_views[r])
        others = [r for r in rows if row_views[r] != 0]
        main = [r for r in rows if row_views[r] == 0]
        if not others or not main or s not in products:
            continue
        query_rows.append(others[-1])
        main_row_of[s] = main[0]

    skus = np.array(list(main_row_of.keys()), dtype=object)
    image_vecs = np.array([mat[main_row_of[s]] for s in skus], dtype=np.float32)

    print(f"\nText-fusion recall  -  {len(skus)} products, {len(query_rows)} held-out-angle queries")
    print("gallery: ONE vector per product (main image), matching production\n")

    print("embedding text for each product (CLIP text tower)...")
    text_vecs = np.array([
        embed_text(f"{products[s]['name']}. {products[s].get('brand','')}. {products[s].get('description','')}"[:300])
        for s in skus
    ], dtype=np.float32)
    print("done.\n")

    results = {}
    baseline, n = recall(query_rows, image_vecs, skus, mat, row_skus, KS)
    results["image-only (baseline)"] = baseline
    for a in ALPHAS:
        fused = a * _norm_rows(image_vecs) + (1 - a) * _norm_rows(text_vecs)
        r, _ = recall(query_rows, fused, skus, mat, row_skus, KS)
        results[f"fused (image*{a:g} + text*{1-a:g})"] = r

    label_w = max(len(k) for k in results) + 2
    print(f"  {'variant':<{label_w}}" + "".join(f"{'recall@'+str(k):>12}" for k in KS))
    for label, r in results.items():
        print(f"  {label:<{label_w}}" + "".join(f"{r[k]*100:>11.1f}%" for k in KS))
    print()


if __name__ == "__main__":
    main()
