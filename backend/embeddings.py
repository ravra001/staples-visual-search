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
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageFilter

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

# Which backend is active for this process. Fixed at import time.
BACKEND = os.environ.get("EMBEDDING_BACKEND", "heuristic").lower()

# Heuristic vector layout: 27 color-histogram bins + 16 shape-occupancy bins
# + 16 edge bins. (Other backends have their own, larger dimensionality.)
HEURISTIC_DIM = 59


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

_clip_state = {}  # lazily-populated: model, preprocess, device


def _get_clip():
    """Lazy-load the CLIP model once per process (weights download on first use)."""
    if _clip_state:
        return _clip_state
    try:
        import torch
        import open_clip
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "EMBEDDING_BACKEND=clip requires torch + open_clip_torch. "
            "Install them with: pip install -r requirements-ml.txt"
        ) from e

    model_name = os.environ.get("CLIP_MODEL", "ViT-B-32")
    pretrained = os.environ.get("CLIP_PRETRAINED", "laion2b_s34b_b79k")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms(
        model_name, pretrained=pretrained, device=device
    )
    model.eval()
    _clip_state.update(model=model, preprocess=preprocess, device=device, torch=torch)
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

    project = os.environ.get("GCP_PROJECT")
    location = os.environ.get("GCP_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("EMBEDDING_BACKEND=vertex requires GCP_PROJECT to be set.")
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
