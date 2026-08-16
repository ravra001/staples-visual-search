"""
EXPERIMENT — benchmark Vertex AI Multimodal Embeddings against local CLIP:
latency, cost, and retrieval quality, on the same product sample.

Protocol: text-to-image recall@k. Gallery = ONE image vector per product
(main image, matching production). Query = the product's own name/brand/
description text. For each backend, does the product's own text retrieve
its own image at rank-1/5/8? Same style of held-out-query eval as
eval_recall.py / eval_text_fusion.py, but comparing embedding BACKENDS
instead of comparing catalog-side strategies within one backend.

Requires:
  - Vertex AI API enabled, and Application Default Credentials available.
    This machine's local gcloud/ADC is broken by TLS interception (see
    GCP_SETUP.md's "Why Cloud Shell, not local gcloud" note) -- run this
    from Cloud Shell, which is already authenticated.
  - embedding.vertex.project in config.yaml, or GCP_PROJECT env var.
  - Local CLIP already configured (same as production) for the CLIP half.

Usage (from backend/): python experimental/eval_vertex_vs_clip.py [--sample N]
"""
import argparse
import json
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
import embeddings

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(BACKEND_DIR, "data", "catalog_abo.json")
IMAGES_DIR = os.path.join(BACKEND_DIR, "static", "images", "products")
KS = (1, 5, 8)
VERTEX_PRICE_PER_IMAGE = 0.0001       # multimodalembedding@001, per input image
VERTEX_PRICE_PER_1M_TOKENS = 0.80     # multimodalembedding@001, text input


def _norm_rows(m):
    m = np.asarray(m, np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def recall(query_vecs, gallery_vecs, skus, ks):
    G = _norm_rows(gallery_vecs)
    Q = _norm_rows(query_vecs)
    scores = Q @ G.T
    hits = {k: 0 for k in ks}
    for i in range(len(skus)):
        ranked = np.argsort(-scores[i])[:max(ks)]
        ranked_skus = [skus[j] for j in ranked]
        for k in ks:
            if skus[i] in ranked_skus[:k]:
                hits[k] += 1
    n = len(skus)
    return {k: hits[k] / n for k in ks}


def embed_vertex_text(text):
    s = embeddings._get_vertex()
    resp = s["model"].get_embeddings(contextual_text=text, dimension=1408)
    return np.asarray(resp.text_embedding, dtype=np.float32)


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    out = fn(*a, **kw)
    return out, time.perf_counter() - t0


def with_retry(fn, *a, retries=2, **kw):
    """Vertex calls cross the network and can transiently 429/500 under a
    tight sequential loop -- one retry with backoff beats aborting the whole
    run over a handful of blips."""
    for attempt in range(retries + 1):
        try:
            return fn(*a, **kw)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))


def stats(latencies):
    arr = np.array(latencies)
    return {"avg_ms": arr.mean() * 1000, "p50_ms": np.percentile(arr, 50) * 1000, "p95_ms": np.percentile(arr, 95) * 1000}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    with open(CATALOG, encoding="utf-8") as f:
        products = json.load(f)
    random.seed(args.seed)
    candidates = random.sample(products, min(args.sample * 2, len(products)))

    skus, texts, images = [], [], []
    for p in candidates:
        if len(skus) >= args.sample:
            break
        fname = os.path.basename(p.get("image_url") or "") or f"{p['sku']}.png"
        path = os.path.join(IMAGES_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            images.append(f.read())
        skus.append(p["sku"])
        texts.append(f"{p['name']}. {p.get('brand', '')}. {p.get('description', '')}"[:300].strip(". "))

    n = len(skus)
    print(f"Sampled {n} products with local images.\n")

    print("Embedding with local CLIP...")
    clip_img_lat, clip_txt_lat = [], []
    clip_img_vecs, clip_txt_vecs = [], []
    for img, txt in zip(images, texts):
        v, dt = timed(embeddings.embed_image, img)
        clip_img_vecs.append(v)
        clip_img_lat.append(dt)
        v, dt = timed(embeddings.embed_text_clip, txt)
        clip_txt_vecs.append(v)
        clip_txt_lat.append(dt)
    clip_img_vecs = np.array(clip_img_vecs, dtype=np.float32)
    clip_txt_vecs = np.array(clip_txt_vecs, dtype=np.float32)

    print("Embedding with Vertex AI (network calls -- this will take a while)...")
    vtx_img_lat, vtx_txt_lat = [], []
    vtx_img_vecs, vtx_txt_vecs = [], []
    for i, (img, txt) in enumerate(zip(images, texts), 1):
        v, dt = timed(with_retry, embeddings._embed_vertex, img)
        vtx_img_vecs.append(v)
        vtx_img_lat.append(dt)
        v, dt = timed(with_retry, embed_vertex_text, txt)
        vtx_txt_vecs.append(v)
        vtx_txt_lat.append(dt)
        if i % 50 == 0:
            print(f"  ...{i}/{n}")
    vtx_img_vecs = np.array(vtx_img_vecs, dtype=np.float32)
    vtx_txt_vecs = np.array(vtx_txt_vecs, dtype=np.float32)

    skus_arr = np.array(skus, dtype=object)
    clip_recall = recall(clip_txt_vecs, clip_img_vecs, skus_arr, KS)
    vtx_recall = recall(vtx_txt_vecs, vtx_img_vecs, skus_arr, KS)

    print("\n=== Retrieval quality: text -> image recall@k (own text finds own image) ===")
    print(f"  {'backend':<10}" + "".join(f"{'recall@' + str(k):>12}" for k in KS))
    print(f"  {'clip':<10}" + "".join(f"{clip_recall[k] * 100:>11.1f}%" for k in KS))
    print(f"  {'vertex':<10}" + "".join(f"{vtx_recall[k] * 100:>11.1f}%" for k in KS))

    print("\n=== Latency per call (this machine / this network) ===")
    for label, lat in [("clip image", clip_img_lat), ("clip text", clip_txt_lat),
                        ("vertex image", vtx_img_lat), ("vertex text", vtx_txt_lat)]:
        st = stats(lat)
        print(f"  {label:<14} avg={st['avg_ms']:.1f}ms  p50={st['p50_ms']:.1f}ms  p95={st['p95_ms']:.1f}ms")

    est_cost = n * VERTEX_PRICE_PER_IMAGE
    print("\n=== Cost (Vertex only -- CLIP is free/local after the one-time model download) ===")
    print(f"  {n} image embed calls x ${VERTEX_PRICE_PER_IMAGE}/image = ${est_cost:.4f}")
    print(f"  (+ {n} short text embed calls, priced per token at ${VERTEX_PRICE_PER_1M_TOKENS}/1M tokens -- negligible)")
    print(f"  Full 10k catalog, one-time embed: ~${10000 * VERTEX_PRICE_PER_IMAGE:.2f}")
    print(f"  Per query at search time (1 image embed call): ~${VERTEX_PRICE_PER_IMAGE} each, vs. $0 marginal for local CLIP")


if __name__ == "__main__":
    main()
