"""
Precompute the catalog embedding index and cache it to disk, so the server
starts instantly even with a large (10k) catalog.

Respects the same config as the app (config.yaml, or env var overrides):
    EMBEDDING_BACKEND = heuristic | clip | vertex   (which model)
    CATALOG_FILE      = data/catalog_abo.json       (which catalog)

Stores TWO vectors per product when text fusion is enabled:
    vecs        — fused (image+text), used for RANKING (better recall — see
                  backend/experimental/eval_text_fusion.py)
    image_vecs  — pure image, used only to DISPLAY a "% match" score to users
                  (interpretable, same scale as before fusion — a pure-image
                  query naturally scores lower against a fused vector, so
                  showing the fused score directly reads as misleadingly low)
See main.py's _display_scores() for how these are combined at query time.

Writes: data/index_<EMBEDDING_BACKEND>.npz  ({skus, vecs, image_vecs, fingerprint}).

Usage (from backend/):
    python build_index.py
    # or, to reuse already-embedded PURE-IMAGE vectors and only (re)compute
    # text fusion (fast — skips the slow image-embedding pass entirely). The
    # source npz's "image_vecs" (or "vecs", if it predates dual-vector storage
    # and text_fusion was off when it was built) must be pure-image vectors:
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


def _load_reusable_image_vectors(path):
    """Load PURE-IMAGE vectors to reuse (skip re-embedding). Prefers the
    "image_vecs" array (dual-vector format); falls back to "vecs" for an
    older cache that predates fusion (in which case vecs WAS pure-image)."""
    if not path or not os.path.exists(path):
        return {}
    d = np.load(path, allow_pickle=True)
    key = "image_vecs" if "image_vecs" in d else "vecs"
    vecs = {s: v for s, v in zip(d["skus"], d[key])}
    print(f"[build_index] reusing {len(vecs)} pure-image vectors from {path} ({key}) — skips re-embedding images")
    return vecs


def main(reuse_vectors_from=None):
    products = [p for p in get_all_products() if os.path.exists(_image_path(p))]
    total = len(products)
    print(f"[build_index] backend={BACKEND} · {total} images to embed "
          f"(text_fusion={'on' if config.TEXT_FUSION_ENABLED else 'off'})")

    reusable = _load_reusable_image_vectors(reuse_vectors_from)

    skus, fused_vecs, image_vecs = [], [], []
    start = time.time()
    for i, p in enumerate(products, 1):
        sku = p["sku"]
        name, brand, desc = p.get("name", ""), p.get("brand", ""), p.get("description", "")
        if sku in reusable:
            image_vec = reusable[sku]
            if config.TEXT_FUSION_ENABLED and BACKEND == "clip":
                text = f"{name}. {brand}. {desc}".strip(". ")
                if text.strip(". "):
                    tvec = embed_text_clip(text[:300])
                    w = config.TEXT_FUSION_IMAGE_WEIGHT
                    fused = _l2norm(w * _l2norm(image_vec) + (1 - w) * _l2norm(tvec))
                else:
                    fused = image_vec
            else:
                fused = image_vec
        else:
            with open(_image_path(p), "rb") as f:
                fused, image_vec = embed_catalog_item(f.read(), name=name, brand=brand,
                                                       description=desc, return_image_vec=True)
        skus.append(sku)
        fused_vecs.append(fused)
        image_vecs.append(image_vec)
        if i % 500 == 0:
            rate = i / (time.time() - start)
            eta = (total - i) / rate if rate else 0
            print(f"[build_index] {i}/{total}  ({rate:.0f}/s, ETA {eta:.0f}s)")

    os.makedirs(CACHE_DIR, exist_ok=True)
    out = os.path.join(CACHE_DIR, f"index_{BACKEND}.npz")
    np.savez(out, skus=np.array(skus, dtype=object),
              vecs=np.array(fused_vecs, dtype=np.float32),
              image_vecs=np.array(image_vecs, dtype=np.float32),
              fingerprint=np.array([config.index_fingerprint()], dtype=object))
    print(f"[build_index] wrote {len(skus)} vectors ({config.index_fingerprint()}) -> {out} in {time.time() - start:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-vectors-from", default=None,
                     help="existing index npz to reuse pure-image vectors from (fast text-fusion-only rebuild)")
    main(ap.parse_args().reuse_vectors_from)
