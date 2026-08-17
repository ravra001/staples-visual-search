"""
Central configuration — loaded from config.yaml.

This replaces the scattered environment variables the app used to read. Values
come from config.yaml; an environment variable of the matching name still wins
if set (so CI/ops can override without editing the file). Point at a different
file with APP_CONFIG=/path/to/other.yaml.

Import this module anywhere you need a setting; it loads the YAML exactly once.
"""
import os

import yaml

_CFG_PATH = os.environ.get("APP_CONFIG") or os.path.join(os.path.dirname(__file__), "config.yaml")


def _load():
    try:
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


_RAW = _load()


def _yaml(path, default=None):
    """Read a dotted path (e.g. 'embedding.clip.model') from the YAML tree."""
    cur = _RAW
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _val(env_name, yaml_path, default=None):
    """Env var wins if set, else YAML, else default."""
    env = os.environ.get(env_name)
    if env is not None and env != "":
        return env
    v = _yaml(yaml_path, None)
    return default if v is None else v


def _bool_val(env_name, yaml_path, default):
    """Like _val, but coerced to bool — an env var override arrives as a
    string ("true"/"1"), while the YAML fallback is already a real bool."""
    return str(_val(env_name, yaml_path, default)).lower() in ("1", "true", "yes")


# ---- embedding ----
EMBEDDING_BACKEND = str(_val("EMBEDDING_BACKEND", "embedding.backend", "heuristic")).lower()
CLIP_MODEL = _val("CLIP_MODEL", "embedding.clip.model", "ViT-B-32")
CLIP_PRETRAINED = _val("CLIP_PRETRAINED", "embedding.clip.pretrained", "laion2b_s34b_b79k")
CLIP_CACHE_DIR = _val("CLIP_CACHE_DIR", "embedding.clip.cache_dir", "models/hf")
CLIP_OFFLINE = str(_val("CLIP_OFFLINE", "embedding.clip.offline", "auto")).lower()
TEXT_FUSION_ENABLED = _bool_val("TEXT_FUSION_ENABLED", "embedding.text_fusion.enabled", False)
TEXT_FUSION_IMAGE_WEIGHT = float(_val("TEXT_FUSION_IMAGE_WEIGHT", "embedding.text_fusion.image_weight", 0.3))
GCP_PROJECT = _val("GCP_PROJECT", "embedding.vertex.project", None)
GCP_LOCATION = _val("GCP_LOCATION", "embedding.vertex.location", "us-central1")

# ---- data ----
DATA_BACKEND = str(_val("DATA_BACKEND", "data.backend", "memory")).lower()
CATALOG_FILE = _val("CATALOG_FILE", "data.catalog_file", None)
DATABASE_URL = _val("DATABASE_URL", "data.database_url", None)

# ---- search ----
TOP_K = int(_val("TOP_K", "search.top_k", 8))
PAGE_SIZE = int(_val("PAGE_SIZE", "search.page_size", 48))
MAX_UPLOAD_MB = float(_val("MAX_UPLOAD_MB", "search.max_upload_mb", 10))
MATCH_THRESHOLD = float(_val("MATCH_THRESHOLD", "search.match_threshold", 55))
CLASSIFIER_ENABLED = _bool_val("CLASSIFIER_ENABLED", "search.category_classifier.enabled", True)
CONF_THRESHOLD = float(_val("CONF_THRESHOLD", "search.category_classifier.confidence_threshold", 45.0))
SOFTMAX_T = float(_val("SOFTMAX_T", "search.category_classifier.softmax_temperature", 0.07))

# ---- index ----
REQUIRE_PREBUILT_INDEX = _bool_val("REQUIRE_PREBUILT_INDEX", "index.require_prebuilt", False)

# ---- server ----
SERVER_HOST = _val("HOST", "server.host", "127.0.0.1")
SERVER_PORT = int(_val("PORT", "server.port", 8000))
SERVER_RELOAD = _bool_val("SERVER_RELOAD", "server.reload", False)
# CORS_ORIGINS is a list, so it doesn't fit _val's single-value env override —
# a CORS_ORIGINS env var (comma-separated) wins if set, else the YAML list.
_cors_env = os.environ.get("CORS_ORIGINS")
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else (_yaml("server.cors_origins", ["*"]) or ["*"])

# ---- product images ----
# Empty (default) = serve from this container's own filesystem, as today.
# Set to a GCS/Cloud CDN origin (no trailing slash, e.g.
# "https://34-120-1-2.sslip.io") to instead redirect /images/products/* to
# it — frees the container's CPU/concurrency budget (shared with CLIP
# inference) from serving static image bytes. See GCP_SETUP.md.
IMAGES_BASE_URL = (_val("IMAGES_BASE_URL", "images.base_url", "") or "").rstrip("/")

# ---- Staples AI chat agent ----
# Reuses GCP_PROJECT/GCP_LOCATION above (same Vertex AI project, whether or
# not EMBEDDING_BACKEND=vertex is active) -- Gemini and the multimodal
# embedding model are separate Vertex APIs, same project/credentials.
AGENT_ENABLED = _bool_val("AGENT_ENABLED", "agent.enabled", True)
AGENT_MODEL = _val("AGENT_MODEL", "agent.model", "gemini-2.0-flash-001")
AGENT_MAX_ITERATIONS = int(_val("AGENT_MAX_ITERATIONS", "agent.max_iterations", 3))


def index_fingerprint(catalog_hash=None):
    """Identity of the model (+ fusion config, + optionally the catalog content)
    an index was built with. A change here means the cached vectors are
    incompatible and must not be ranked against — covers the model itself,
    catalog-side text fusion (a fused vector is 70% a function of the product's
    name/brand/description, so editing that text invalidates the vector even
    though the model didn't change), and optionally the catalog content itself
    via catalog_hash (pass a hash of the loaded products' sku+name+brand+
    description so a text edit or catalog swap is detected, not silently
    ranked against stale text)."""
    fusion = f":fusion={TEXT_FUSION_IMAGE_WEIGHT:g}" if TEXT_FUSION_ENABLED else ""
    cat = f":cat={catalog_hash}" if catalog_hash else ""
    if EMBEDDING_BACKEND == "clip":
        return f"clip:{CLIP_MODEL}:{CLIP_PRETRAINED}{fusion}{cat}"
    if EMBEDDING_BACKEND == "vertex":
        return f"vertex:multimodalembedding@001{fusion}{cat}"
    return f"heuristic:v1{cat}"
