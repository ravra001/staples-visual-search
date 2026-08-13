# Running the Staples Visual Search demo (10,000 products) on another computer

This package is **self-contained**: it already includes the 10,000-product
catalog, all 10k real product images, and the **prebuilt visual-search indexes**,
so you do **not** need to download the dataset or spend ~12 minutes rebuilding
anything. Just install Python packages and run.

There are two ways to run it:

| Mode | Photo-search quality | Install weight | Internet needed |
|------|----------------------|----------------|-----------------|
| **A. Heuristic** (default) | Colour/shape similarity — decent | tiny (~30 MB) | none |
| **B. CLIP** (recommended) | Real semantic similarity — great | large (~2 GB) | yes, first run only (downloads the model) |

Both use the same 10k catalog and the same UI. Start with **Mode A** to confirm
it runs, then switch to **Mode B** for the good search.

---

## What's in this package

```
staples-visual-search/
├─ backend/
│  ├─ main.py, embeddings.py, products_data.py, ...
│  ├─ data/
│  │  ├─ catalog_abo.json        # the 10,000 products (real names + metadata)
│  │  ├─ index_heuristic.npz     # prebuilt index for Mode A
│  │  └─ index_clip.npz          # prebuilt index for Mode B (10k CLIP vectors)
│  └─ static/images/products/    # ~10,000 real product images (.jpg) + demo (.png)
├─ frontend/                     # the web UI (no build step, no Node)
├─ requirements.txt              # base packages (Mode A)
├─ requirements-ml.txt           # extra packages for CLIP / GCP (Mode B)
├─ README.md                     # full architecture / design notes
└─ RUN_10K.md                    # this file
```

---

## Prerequisites

- **Python 3.9 or newer** (tested on 3.13). Check with `python --version`.
- **~1 GB free disk** for Mode A, **~3 GB** for Mode B (PyTorch + the CLIP model).
- A web browser.
- For **Mode B only**: internet access on the first run (to download the CLIP
  model weights, ~350 MB, cached after that).

> No Node.js, no database, no cloud account, and no Docker are required.

---

## Step 1 — Create a virtual environment (recommended)

**Windows (PowerShell):**
```powershell
cd staples-visual-search
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux (bash):**
```bash
cd staples-visual-search
python3 -m venv .venv
source .venv/bin/activate
```

---

## Step 2 — Install packages

### Mode A (heuristic) — minimal
```bash
pip install -r requirements.txt
```

### Mode B (CLIP) — adds the real model
Install the base packages, the CPU build of PyTorch (smaller, no GPU needed),
and the ML extras:
```bash
pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-ml.txt
```

---

## Step 3 — Run the server (with the 10k catalog)

The catalog is selected with the `CATALOG_FILE` environment variable, and the
model with `EMBEDDING_BACKEND`. **Setting environment variables differs by OS** —
use the block for your system.

### Mode A — Heuristic (fast, no model download)

**Windows (PowerShell):**
```powershell
cd backend
$env:CATALOG_FILE = "data/catalog_abo.json"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**macOS / Linux (bash):**
```bash
cd backend
CATALOG_FILE=data/catalog_abo.json python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### Mode B — CLIP (real semantic search)

**Windows (PowerShell):**
```powershell
cd backend
$env:CATALOG_FILE = "data/catalog_abo.json"
$env:EMBEDDING_BACKEND = "clip"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**macOS / Linux (bash):**
```bash
cd backend
CATALOG_FILE=data/catalog_abo.json EMBEDDING_BACKEND=clip python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

On the **first** Mode B run, the CLIP model weights download once (you'll see
Hugging Face progress). Every run after that is offline and instant, because the
catalog index is already prebuilt.

---

## Step 4 — Open it

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

---

## Important: keep the CLIP model consistent

`index_clip.npz` was built with the **default** OpenCLIP model
(`ViT-B-32` / `laion2b_s34b_b79k`). The app uses that same default, so the query
image and the prebuilt catalog vectors match. **Do not set `CLIP_PRETRAINED` to a
different value** — if you do, rebuild the index (see below), or search quality
will be wrong.

---

## Troubleshooting

- **`Address already in use` / port 8000 busy** — use another port, e.g. add
  `--port 8001`, and open `http://localhost:8000` → `:8001`.
- **PowerShell "running scripts is disabled"** when activating the venv — run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window, then
  activate again. (Or skip the venv and `pip install` globally.)
- **CLIP model download fails with `CERTIFICATE_VERIFY_FAILED`** (corporate
  networks that inspect TLS) — this is already handled: `requirements-ml.txt`
  installs `truststore`, which the app uses to trust your OS certificate store.
  Make sure `pip install -r requirements-ml.txt` succeeded.
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
