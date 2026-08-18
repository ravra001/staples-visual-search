# Running the Staples Visual Search demo (10,000 products) on another computer

This package is **self-contained**: it already includes the 10,000-product
catalog, all 10k real product images, and the **prebuilt visual-search indexes**,
so you do **not** need to download the dataset or spend ~12 minutes rebuilding
anything. Just install Python packages and run.

There are two ways to run it:

| Mode | Photo-search quality | Install weight | Internet needed |
|------|----------------------|----------------|-----------------|
| **A. Heuristic** (default) | Colour/shape similarity — decent | tiny (~30 MB) | none |
| **B. CLIP** (recommended) | Real semantic similarity — great | large (~2 GB) | no — model ships bundled, fully offline (see below) |

Both use the same 10k catalog and the same UI. Start with **Mode A** to confirm
it runs, then switch to **Mode B** for the good search.

---

## What's in this package

```
staples-visual-search/
├─ backend/
│  ├─ config.yaml                # ALL configuration lives here (no env vars)
│  ├─ run.py                     # entry point (reads config.yaml)
│  ├─ main.py, embeddings.py, products_data.py, config.py, ...
│  ├─ data/
│  │  ├─ catalog_abo.json        # the 10,000 products (real names + metadata)
│  │  ├─ index_heuristic.npz     # prebuilt index for Mode A
│  │  └─ index_clip.npz          # prebuilt index for Mode B (10k CLIP vectors)
│  ├─ models/hf/                 # bundled CLIP model (577 MB) → Mode B runs OFFLINE
│  └─ static/images/products/    # ~10,000 real product images (.jpg) + demo (.png)
├─ frontend/                     # the web UI (no build step, no Node)
├─ pyproject.toml + uv.lock      # uv project + locked dependencies
├─ requirements.txt              # pip alternative (base packages)
├─ requirements-ml.txt           # pip alternative (CLIP / GCP / SQL extras)
├─ README.md                     # full architecture / design notes
└─ RUN_10K.md                    # this file
```

---

## Prerequisites

- **Python 3.9 or newer** (tested on 3.13). Check with `python --version`.
- **~1 GB free disk** for Mode A, **~3 GB** for Mode B (PyTorch + the CLIP model).
- A web browser.
- **No internet needed at runtime.** The CLIP model is bundled in
  `backend/models/hf/`, so Mode B runs fully offline (see below).

> No Node.js, no database, no cloud account, and no Docker are required.

### Offline CLIP model (no HuggingFace downloads)

The CLIP model weights ship inside the package at `backend/models/hf/`. On
startup the app points HuggingFace's cache there and sets `HF_HUB_OFFLINE=1`
automatically **when it sees the model present** — so Mode B makes **zero**
network calls at runtime (no download, not even a metadata check).

- **Keep the `backend/models/hf/` folder** when you copy/unzip the app — that's
  what makes it offline. (It's ~577 MB; if it's missing, Mode B will try to
  download the model once on first use instead, which needs internet.)
- To force a re-download or update the model, run with `HF_HUB_OFFLINE=0`.
- Mode A (heuristic) never touches HuggingFace at all.

---

## Configuration — one file, no environment variables

Everything is configured in **`backend/config.yaml`** — which catalog, which
embedding backend, the classifier, the server host/port. The package already
ships set to **Mode B (CLIP) + the 10k catalog**, so you don't need to change
anything. Key settings:

```yaml
embedding:
  backend: clip                     # heuristic | clip | vertex
data:
  backend: memory
  catalog_file: data/catalog_abo.json   # the 10k set (null = 30-item demo)
server:
  host: 127.0.0.1
  port: 8000
```

To run **Mode A (heuristic, no model needed)** instead, just set
`embedding.backend: heuristic`. (Env vars of the same name still override the
file if you ever need them — e.g. `EMBEDDING_BACKEND`, `CATALOG_FILE`, `PORT`.)

---

## Step 1 — Install (choose one)

### Option A — `uv` (recommended)
[uv](https://docs.astral.sh/uv/) creates the virtualenv and installs locked
dependencies in one step. Install uv (`pip install uv`, or see their site), then:

```bash
cd staples-visual-search
uv sync                 # Mode A (heuristic) — base packages only
uv sync --extra clip    # Mode B (CLIP)      — also installs PyTorch + OpenCLIP
```

`uv sync` reads `pyproject.toml` / `uv.lock`, creates `.venv`, and installs the
exact pinned versions. (It's already configured to pull the CPU build of PyTorch
and to use your OS certificate store.)

### Option B — `pip` + venv
```bash
cd staples-visual-search
python -m venv .venv
# Windows:  .\.venv\Scripts\Activate.ps1     macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt              # Mode A
pip install torch --index-url https://download.pytorch.org/whl/cpu   # Mode B: CPU torch
pip install -r requirements-ml.txt           # Mode B: CLIP extras
```

---

## Step 2 — Run

With **uv** (no venv activation needed):
```bash
uv run python backend/run.py
```

With **pip/venv** (activate the venv first, then):
```bash
cd backend && python run.py
```

`run.py` reads the host/port (and everything else) from `config.yaml`. That's it —
no environment variables, no long command line. Mode B loads the **bundled** CLIP
model from `backend/models/hf/` and runs **fully offline**; the catalog index is
prebuilt, so startup is instant.

---

## Step 3 — Open it

Go to **http://localhost:8000** in your browser.

Confirm what's running (the header badge shows this too):
```
http://localhost:8000/api/config
```
- Mode A → `{"embedding_backend":"heuristic","data_backend":"memory"}`
- Mode B → `{"embedding_backend":"clip","data_backend":"memory"}` (badge turns green)

Try it: click the **camera icon** in the search bar (or the hero "Try Visual
Search" button), pick any furniture / lighting / décor / office photo, and you'll
get visually similar products ranked by match score.

**"Staples AI" chat runs in degraded mode without a GCP project.** The homepage's
right-hand "Staples AI" column and its chat panel need a real Vertex AI (Gemini)
connection — `embedding.vertex.project` in `config.yaml` or `GCP_PROJECT` — which
this local-only setup doesn't have. Without it, every chat message still returns
a real answer (it silently falls back to plain hybrid search, `degraded: true` in
the response), just without the model's tool-calling — no office/room planning,
deals, comparisons, or receipt reading. This is expected, not a bug; see
`GCP_SETUP.md` if you want the full chat experience locally too.

---

## Important: keep the CLIP model consistent

`index_clip.npz` was built with the model in `config.yaml`
(`embedding.clip.model: ViT-B-32` / `pretrained: laion2b_s34b_b79k`), so the
query image and the prebuilt catalog vectors match. The index is **fingerprinted**
with that model — if you change either value without rebuilding, the app detects
the mismatch on startup and **refuses the stale cache** (it won't silently return
wrong results). To actually change models, update `config.yaml` and rebuild the
index (see below).

---

## Troubleshooting

- **`Address already in use` / port 8000 busy** — use another port, e.g. add
  `--port 8001`, and open `http://localhost:8000` → `:8001`.
- **PowerShell "running scripts is disabled"** when activating the venv — run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window, then
  activate again. (Or skip the venv and `pip install` globally.)
- **CLIP tries to download / can't reach HuggingFace** — the offline model
  folder `backend/models/hf/` is probably missing from your copy. Restore it, or
  run once with `HF_HUB_OFFLINE=0` on a machine with internet to fetch it. (If a
  download is ever needed on a TLS-inspecting corporate network and fails with
  `CERTIFICATE_VERIFY_FAILED`, that's handled by `truststore` from
  `requirements-ml.txt`, which trusts your OS certificate store.)
- **`torch` won't install** — you're likely on an unsupported Python. Use Python
  3.9–3.13, and the CPU index URL shown in Step 2.
- **Broken image on a few product cards** — ~33 of the 10k had no source image;
  harmless.
- **Old UI after an update** — hard-refresh the browser (Ctrl+F5).

---

## Optional

**Rebuild an index** (only if you change the catalog or CLIP model):
```bash
# from backend/
CATALOG_FILE=data/catalog_abo.json python build_index.py                       # heuristic
EMBEDDING_BACKEND=clip CATALOG_FILE=data/catalog_abo.json python build_index.py # clip (~12 min on CPU)
```

**Run the small 30-item demo catalog instead** — just omit `CATALOG_FILE`.

**Cloud backends (Vertex AI / Cloud SQL)** — see `README.md`; not needed to run locally.
