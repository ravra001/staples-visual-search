"""
EXPERIMENT — live serving for the 3-way method comparison page
(frontend/compare-methods.html). Not part of production ranking; entirely
self-contained and only mounted by main.py if the experimental data files
exist (see main.py's optional import).

All three methods rank the SAME 497-product subset (so it's a fair,
apples-to-apples comparison, not "500 items" vs "the full 10k catalog"):
  baseline    single main-image vector per product
  multiview   every view per product; score = MAX cosine over its views
              (this is what "any angle matches" retrieval looks like)
  fusion      single main image, fused with text — same recipe production uses

See build_compare_index.py / build_multiview_index.py for how the vectors are
built. Nothing here touches main.py's production index or ranking.
"""
import json
import os

import numpy as np

import config
from embeddings import BACKEND as EMBEDDING_BACKEND, embed_image

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BACKEND_DIR, "data")
COMPARE_NPZ = os.path.join(DATA_DIR, "index_compare.npz")
MULTIVIEW_NPZ = os.path.join(DATA_DIR, "index_multiview.npz")
CATALOG_JSON = os.path.join(DATA_DIR, "catalog_multiview_text.json")

READY = all(os.path.exists(p) for p in (COMPARE_NPZ, MULTIVIEW_NPZ, CATALOG_JSON))

# The model these experimental indices were built with — compared against the
# LIVE embedding model at load time, so a model change (e.g. EMBEDDING_BACKEND
# flipped, or CLIP_MODEL/CLIP_PRETRAINED changed) is detected instead of
# silently ranking a new-model query against old-model vectors and presenting
# it as a fair method comparison.
_EXPECTED_MODEL = f"clip:{config.CLIP_MODEL}:{config.CLIP_PRETRAINED}"

_state = {}
_load_error = None


def _norm_rows(m):
    m = np.asarray(m, np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


class CompareUnavailable(Exception):
    """Raised when the experimental data exists but doesn't match the live
    model, or is otherwise unusable — distinct from READY=False (missing
    files), so the endpoint can report a clear reason instead of a raw 500."""


def _check_fingerprint(npz, path):
    fp = str(npz["fingerprint"][0]) if "fingerprint" in npz else None
    if fp != _EXPECTED_MODEL:
        raise CompareUnavailable(
            f"{os.path.basename(path)} was built with {fp!r}, but the live model is "
            f"{_EXPECTED_MODEL!r}. Rebuild it (see backend/experimental/) before comparing."
        )


def _load():
    if _state or _load_error:
        if _load_error:
            raise _load_error
        return _state
    try:
        if EMBEDDING_BACKEND != "clip":
            raise CompareUnavailable(
                f"The compare tool needs EMBEDDING_BACKEND=clip (experimental data is CLIP-only); "
                f"the live server is running {EMBEDDING_BACKEND!r}.")

        with open(CATALOG_JSON, encoding="utf-8") as f:
            products = {p["sku"]: p for p in json.load(f)}

        c = np.load(COMPARE_NPZ, allow_pickle=True)
        _check_fingerprint(c, COMPARE_NPZ)
        baseline_skus = np.array(c["skus"], dtype=object)
        baseline_matrix = _norm_rows(c["single_vecs"])
        fusion_matrix = _norm_rows(c["fused_vecs"])

        mv = np.load(MULTIVIEW_NPZ, allow_pickle=True)
        mv_fp = str(mv["fingerprint"][0]) if "fingerprint" in mv else None
        if not (mv_fp or "").startswith(f"clip:{config.CLIP_MODEL}:{config.CLIP_PRETRAINED}"):
            raise CompareUnavailable(
                f"{os.path.basename(MULTIVIEW_NPZ)} was built with {mv_fp!r}, but the live model "
                f"needs clip:{config.CLIP_MODEL}:{config.CLIP_PRETRAINED}. Rebuild it.")
        mv_skus = np.array(mv["row_skus"], dtype=object)
        mv_matrix = _norm_rows(mv["vecs"])

        dims = {baseline_matrix.shape[1] if baseline_matrix.size else None,
                fusion_matrix.shape[1] if fusion_matrix.size else None,
                mv_matrix.shape[1] if mv_matrix.size else None}
        if len(dims - {None}) > 1:
            raise CompareUnavailable(f"Dimension mismatch across the compare indices: {dims}. Rebuild them together.")

        _state.update(
            products=products,
            baseline_skus=baseline_skus, baseline_matrix=baseline_matrix,
            fusion_matrix=fusion_matrix,
            mv_skus=mv_skus, mv_matrix=mv_matrix,
        )
        print(f"[compare_search] loaded {len(products)} products for the 3-way method comparison tool")
    except CompareUnavailable as e:
        globals()["_load_error"] = e
        raise
    return _state


def _thumb(sku):
    p = _state["products"].get(sku, {})
    views = p.get("views") or []
    return f"/images/multiview/{views[0]}" if views else None


def _rank_single(matrix, skus, qn, k):
    scores = matrix @ qn
    k = min(k, scores.shape[0])
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(str(skus[i]), float(scores[i])) for i in top]


def _rank_multiview(qn, k):
    """Max-score-over-views per product — 'any angle matches'."""
    scores = _state["mv_matrix"] @ qn
    best = {}
    for sku, sc in zip(_state["mv_skus"], scores):
        s = str(sku)
        if sc > best.get(s, -1e9):
            best[s] = float(sc)
    ranked = sorted(best.items(), key=lambda kv: -kv[1])[:k]
    return ranked


def _to_items(ranked):
    out = []
    for sku, score in ranked:
        p = _state["products"].get(sku, {})
        out.append({
            "sku": sku,
            "name": p.get("name", sku),
            "category": p.get("category", ""),
            "image_url": _thumb(sku),
            "match_score": round(score * 100, 1),
        })
    return out


def compare(image_bytes: bytes, k: int = 8):
    """Run all three methods against the same query image. Returns a dict of
    method -> list of result items (see _to_items)."""
    _load()
    qn_raw = embed_image(image_bytes)
    qn = qn_raw / (np.linalg.norm(qn_raw) + 1e-8)

    baseline = _rank_single(_state["baseline_matrix"], _state["baseline_skus"], qn, k)
    fusion = _rank_single(_state["fusion_matrix"], _state["baseline_skus"], qn, k)
    multiview = _rank_multiview(qn, k)

    return {
        "baseline": _to_items(baseline),
        "multiview": _to_items(multiview),
        "fusion": _to_items(fusion),
        "catalog_size": len(_state["products"]),
    }
