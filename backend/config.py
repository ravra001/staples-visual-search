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


# ---- embedding ----
EMBEDDING_BACKEND = str(_val("EMBEDDING_BACKEND", "embedding.backend", "heuristic")).lower()
CLIP_MODEL = _val("CLIP_MODEL", "embedding.clip.model", "ViT-B-32")
CLIP_PRETRAINED = _val("CLIP_PRETRAINED", "embedding.clip.pretrained", "laion2b_s34b_b79k")
CLIP_CACHE_DIR = _val("CLIP_CACHE_DIR", "embedding.clip.cache_dir", "models/hf")
CLIP_OFFLINE = str(_val("CLIP_OFFLINE", "embedding.clip.offline", "auto")).lower()
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
CLASSIFIER_ENABLED = bool(_yaml("search.category_classifier.enabled", True))
CONF_THRESHOLD = float(_val("CONF_THRESHOLD", "search.category_classifier.confidence_threshold", 45.0))
SOFTMAX_T = float(_val("SOFTMAX_T", "search.category_classifier.softmax_temperature", 0.07))

# ---- index ----
REQUIRE_PREBUILT_INDEX = str(_val("REQUIRE_PREBUILT_INDEX", "index.require_prebuilt", False)).lower() in ("1", "true", "yes")

# ---- server ----
SERVER_HOST = _val("HOST", "server.host", "127.0.0.1")
SERVER_PORT = int(_val("PORT", "server.port", 8000))
SERVER_RELOAD = bool(_yaml("server.reload", False))
CORS_ORIGINS = _yaml("server.cors_origins", ["*"]) or ["*"]


def index_fingerprint():
    """Identity of the model an index was built with. A change here means the
    cached vectors are incompatible and must not be ranked against."""
    if EMBEDDING_BACKEND == "clip":
        return f"clip:{CLIP_MODEL}:{CLIP_PRETRAINED}"
    if EMBEDDING_BACKEND == "vertex":
        return "vertex:multimodalembedding@001"
    return "heuristic:v1"
