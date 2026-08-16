"""
Image embeddings for visual search — pluggable backends.

The embedding is the heart of "search by photo," and it's the piece most
likely to be swapped as the project matures. So this module exposes ONE stable
interface — `embed_image(image_bytes) -> np.ndarray` — backed by any of three
interchangeable providers, selected at runtime via the EMBEDDING_BACKEND env
var:

  heuristic  (default)  Dependency-free color + shape + edge histogram. Runs
                        anywhere with zero setup. Good for distinguishing clean,
                        shape-distinct studio product shots; does NOT generalize
                        to real-world photos (odd angles, lighting, clutter).

  clip                  Local OpenCLIP model (ViT-B-32). Real learned visual
                        embedding with genuine real-world generalization, still
                        100% offline / zero-cloud. Requires `torch` +
                        `open_clip_torch` (see requirements-ml.txt).

  vertex                Google Vertex AI Multimodal Embeddings (1408-dim).
                        The production target. Requires a GCP project +
                        credentials and `google-cloud-aiplatform`.

`cosine_similarity()` and `top_k_matches()` are backend-agnostic — they don't
care about vector dimensionality — so switching backends changes nothing
downstream. The ONLY rule: the catalog index and the query must be embedded by
the same backend within a single process (guaranteed here, since the backend is
fixed per process at startup).
"""
import io
import os
import threading
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageFilter

import config

# --- Keep the CLIP model bundled WITH the app and run it fully offline ---
# Point HuggingFace's cache at the project-local folder from config
# (embedding.clip.cache_dir) so the model travels inside the app package rather
# than the user's home dir. offline mode (embedding.clip.offline):
#   auto  -> offline when the model is present in the cache (recommended)
#   true  -> always offline;  false -> allow downloads.
# setdefault is used so real env vars still win.
_HF_LOCAL = config.CLIP_CACHE_DIR if os.path.isabs(config.CLIP_CACHE_DIR) \
    else os.path.join(os.path.dirname(__file__), config.CLIP_CACHE_DIR)
os.environ.setdefault("HF_HOME", _HF_LOCAL)
_hf_hub = os.path.join(_HF_LOCAL, "hub")
_model_present = os.path.isdir(_hf_hub) and any(n.startswith("models--") for n in os.listdir(_hf_hub))
if config.CLIP_OFFLINE == "true" or (config.CLIP_OFFLINE == "auto" and _model_present):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

# On networks that intercept TLS (corporate proxies / AV), Python's bundled CA
# set won't trust the interception cert, so model-weight downloads for the clip
# and vertex backends fail with CERTIFICATE_VERIFY_FAILED. `truststore` makes
# Python use the OS trust store (which those environments already configure).
# Optional + best-effort: harmless if not installed or not needed.
try:  # pragma: no cover
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# Which backend is active for this process. Fixed at import time (from config).
BACKEND = config.EMBEDDING_BACKEND

# Heuristic vector layout: 27 color-histogram bins + 16 shape-occupancy bins
# + 16 edge bins. (Other backends have their own, larger dimensionality.)
HEURISTIC_DIM = 59

# Known output dims per CLIP architecture (a property of the model, not the
# pretrained weights) — lets us declare a fixed-width pgvector column without
# having to load the model just to ask it its own output shape.
_CLIP_DIMS = {
    "ViT-B-32": 512, "ViT-B-16": 512, "ViT-L-14": 768, "ViT-L-14-336": 768,
    "ViT-H-14": 1024, "ViT-g-14": 1024, "ViT-bigG-14": 1280,
}
VERTEX_DIM = 1408  # multimodalembedding@001's fixed output size


def embedding_dim() -> int:
    """Output dimensionality of the ACTIVE backend. Needed to declare a
    fixed-width pgvector column (Postgres, unlike a NumPy array, needs the
    vector width known at CREATE TABLE time, not just at insert time)."""
    if BACKEND == "heuristic":
        return HEURISTIC_DIM
    if BACKEND == "clip":
        return _CLIP_DIMS.get(config.CLIP_MODEL, 512)
    if BACKEND == "vertex":
        return VERTEX_DIM
    raise RuntimeError(f"Unknown EMBEDDING_BACKEND={BACKEND!r}")


def _load_image(image_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


# --------------------------------------------------------------------------
# Backend 1: heuristic (default, zero-dependency)
# --------------------------------------------------------------------------

def _embed_heuristic(image_bytes: bytes) -> np.ndarray:
    """Deterministic feature vector: foreground color histogram + edge/shape energy.

    Background pixels (near-white studio background) are excluded from the color
    histogram so the vector reflects the object rather than the empty frame.
    """
    img = _load_image(image_bytes)
    img_small = img.resize((64, 64))
    arr = np.asarray(img_small).astype(np.float32) / 255.0  # (64,64,3)

    # foreground mask: pixels that differ meaningfully from near-white background
    brightness = arr.mean(axis=-1)
    color_spread = arr.max(axis=-1) - arr.min(axis=-1)
    is_background = (brightness > 0.85) & (color_spread < 0.08)
    fg_mask = ~is_background

    # foreground color histogram, coarse (3x3x3 = 27 dims)
    bins = 3
    idx = np.minimum((arr * bins).astype(int), bins - 1)
    flat_idx = idx[..., 0] * bins * bins + idx[..., 1] * bins + idx[..., 2]
    fg_flat_idx = flat_idx[fg_mask]
    if fg_flat_idx.size == 0:
        fg_flat_idx = flat_idx.ravel()  # fully-background fallback
    hist = np.bincount(fg_flat_idx.ravel(), minlength=bins ** 3).astype(np.float32)
    hist /= hist.sum() + 1e-8

    # foreground shape: coarse occupancy grid (16 dims)
    fg_grid = fg_mask.astype(np.float32).reshape(8, 8, 8, 8).mean(axis=(1, 3))
    fg_grid = fg_grid.reshape(16, 4).mean(axis=1)
    fg_grid /= fg_grid.sum() + 1e-8

    # edge energy per spatial grid cell (16 dims)
    gray = img_small.convert("L").filter(ImageFilter.FIND_EDGES)
    gray_arr = np.asarray(gray).astype(np.float32) / 255.0
    edge_grid = gray_arr.reshape(8, 8, 8, 8).mean(axis=(1, 3)).ravel()
    edge_grid = edge_grid.reshape(16, 4).mean(axis=1)
    edge_grid /= edge_grid.sum() + 1e-8

    vec = np.concatenate([hist * 1.0, fg_grid * 3.0, edge_grid * 2.0]).astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


# --------------------------------------------------------------------------
# Backend 2: local OpenCLIP (real model, still offline)
# --------------------------------------------------------------------------

_clip_state = {}  # lazily-populated: model, preprocess, device, tokenizer
_clip_load_lock = threading.Lock()


def _get_clip():
    """Lazy-load the CLIP model once per process (weights download on first use).

    Guarded by a lock + double-check: without it, two concurrent first-requests
    (plausible under Cloud Run's --concurrency=20, e.g. a demo presenter and a
    QR-scanning phone hitting cold start at once) would each load their own
    full copy of the model into a memory-constrained instance.
    """
    if _clip_state:
        return _clip_state
    with _clip_load_lock:
        if _clip_state:  # someone else finished loading while we waited for the lock
            return _clip_state
        try:
            import torch
            import open_clip
        except ImportError as e:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "EMBEDDING_BACKEND=clip requires torch + open_clip_torch. "
                "Install them with: pip install -r requirements-ml.txt"
            ) from e

        model_name = config.CLIP_MODEL
        pretrained = config.CLIP_PRETRAINED
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, device=device
        )
        model.eval()
        tokenizer = open_clip.get_tokenizer(model_name)
        _clip_state.update(model=model, preprocess=preprocess, device=device, torch=torch, tokenizer=tokenizer)
    return _clip_state


def _embed_clip(image_bytes: bytes) -> np.ndarray:
    s = _get_clip()
    torch = s["torch"]
    img = _load_image(image_bytes)
    tensor = s["preprocess"](img).unsqueeze(0).to(s["device"])
    with torch.no_grad():
        feats = s["model"].encode_image(tensor)
        feats = feats / feats.norm(dim=-1, keepdim=True)  # L2-normalize
    return feats.cpu().numpy().astype(np.float32)[0]


def embed_text_clip(text: str) -> np.ndarray:
    """CLIP text-tower embedding (same joint space as embed_image), L2-normalized.
    Catalog-side only — see embed_catalog_item(). Requires the clip backend."""
    s = _get_clip()
    torch = s["torch"]
    tokens = s["tokenizer"]([text]).to(s["device"])
    with torch.no_grad():
        feats = s["model"].encode_text(tokens)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.cpu().numpy().astype(np.float32)[0]


def _l2norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def embed_catalog_item(image_bytes: bytes, name: str = "", brand: str = "", description: str = "",
                        return_image_vec: bool = False):
    """Embed a CATALOG product: image, optionally fused with its text (name/brand/
    description) per config.TEXT_FUSION_*. Validated (recall eval) to beat
    image-only retrieval — see backend/experimental/eval_text_fusion.py.

    The QUERY side never calls this — a user only supplies a photo, so
    visual_search() always uses plain embed_image(). Fusion only enriches what's
    stored for each catalog item, matching Amazon's published approach (fuse on
    the catalog side; compare a pure-image query against it).

    Returns the fused vector (used for RANKING). If return_image_vec=True,
    returns (fused_vec, image_vec) instead — the raw image vector is kept
    alongside so callers can show a pure-image similarity score to users
    (interpretable, same scale as before fusion) while still ranking by the
    fused vector (better recall). See main.py's dual-score display.

    Only supported on the clip backend today; other backends fall back to
    image-only (no error — fusion is an enhancement, not a requirement).
    """
    image_vec = embed_image(image_bytes)
    text = f"{name}. {brand}. {description}".strip(". ")
    if BACKEND == "clip" and config.TEXT_FUSION_ENABLED and text.strip(". "):
        text_vec = embed_text_clip(text[:300])
        w = config.TEXT_FUSION_IMAGE_WEIGHT
        fused = _l2norm(w * _l2norm(image_vec) + (1 - w) * _l2norm(text_vec))
    else:
        fused = image_vec
    return (fused, image_vec) if return_image_vec else fused


# --------------------------------------------------------------------------
# Backend 3: Vertex AI Multimodal Embeddings (production target)
# --------------------------------------------------------------------------

_vertex_state = {}


def _get_vertex():
    if _vertex_state:
        return _vertex_state
    try:
        import vertexai
        from vertexai.vision_models import MultiModalEmbeddingModel
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "EMBEDDING_BACKEND=vertex requires google-cloud-aiplatform. "
            "Install it with: pip install -r requirements-ml.txt"
        ) from e

    project = config.GCP_PROJECT
    location = config.GCP_LOCATION
    if not project:
        raise RuntimeError("vertex backend requires embedding.vertex.project in config.yaml (or GCP_PROJECT).")
    vertexai.init(project=project, location=location)
    model = MultiModalEmbeddingModel.from_pretrained("multimodalembedding@001")
    _vertex_state.update(model=model, MMImage=_import_vertex_image())
    return _vertex_state


def _import_vertex_image():
    from vertexai.vision_models import Image as VertexImage
    return VertexImage


def _embed_vertex(image_bytes: bytes) -> np.ndarray:
    s = _get_vertex()
    vertex_image = s["MMImage"](image_bytes=image_bytes)
    embeddings = s["model"].get_embeddings(image=vertex_image, dimension=1408)
    return np.asarray(embeddings.image_embedding, dtype=np.float32)


# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------

_BACKENDS = {
    "heuristic": _embed_heuristic,
    "clip": _embed_clip,
    "vertex": _embed_vertex,
}


def embed_image(image_bytes: bytes) -> np.ndarray:
    """Embed an image with the active backend (set via EMBEDDING_BACKEND)."""
    fn = _BACKENDS.get(BACKEND)
    if fn is None:
        raise RuntimeError(
            f"Unknown EMBEDDING_BACKEND={BACKEND!r}. "
            f"Choose one of: {', '.join(_BACKENDS)}."
        )
    return fn(image_bytes)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8
    return float(np.dot(a, b) / denom)


def top_k_matches(query_vec: np.ndarray, catalog: List[Tuple[str, np.ndarray]], k: int = 8):
    """catalog: list of (sku, vector). Returns list of (sku, similarity) sorted desc."""
    scored = [(sku, cosine_similarity(query_vec, vec)) for sku, vec in catalog]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
